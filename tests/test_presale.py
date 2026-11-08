import pytest

from curvebot import db, service


@pytest.fixture
def conn(tmp_path):
    with db.connect(str(tmp_path / "test.db")) as c:
        yield c


class TestDeposits:
    def test_deposit_and_contribution(self, conn):
        service.record_deposit(conn, 1, 2.0, "sig1")
        service.record_deposit(conn, 1, 1.0, "sig2")
        assert db.user_contribution(conn, 1) == 3.0
        assert db.total_raised(conn) == 3.0

    def test_dedup_key_includes_index(self, conn):
        db.mark_sig(conn, "sigA:0")
        assert db.sig_seen(conn, "sigA:0")
        assert not db.sig_seen(conn, "sigA:1")

    def test_shares(self, conn):
        service.record_deposit(conn, 1, 3.0)
        service.record_deposit(conn, 2, 1.0)
        assert service.shares(conn) == {1: 3.0, 2: 1.0}


class TestRefundReservation:
    def test_reserve_commit(self, conn):
        service.record_deposit(conn, 1, 2.0)
        q = service.reserve_refund(conn, 1, 0.5, 30)
        assert q["refund_sol"] == pytest.approx(1.0)
        assert q["fee_sol"] == pytest.approx(0.003)
        assert q["net_sol"] == pytest.approx(0.997)
        # reserved: available drops immediately
        assert service.available_contribution(conn, 1) == pytest.approx(1.0)
        # contribution NOT yet in ledger
        assert db.user_contribution(conn, 1) == 2.0
        service.commit_refund(conn, q["pending_id"], signature="s")
        assert db.user_contribution(conn, 1) == pytest.approx(1.0)
        assert service.available_contribution(conn, 1) == pytest.approx(1.0)

    def test_reserve_release(self, conn):
        service.record_deposit(conn, 1, 2.0)
        q = service.reserve_refund(conn, 1, 0.5, 30)
        service.release_refund(conn, q["pending_id"])
        assert service.available_contribution(conn, 1) == pytest.approx(2.0)
        assert db.user_contribution(conn, 1) == 2.0

    def test_concurrent_reservations_cannot_overdraw(self, conn):
        """Two simultaneous 100% refund requests: the second must fail
        or be capped at what is actually available."""
        service.record_deposit(conn, 1, 1.0)
        q1 = service.reserve_refund(conn, 1, 1.0, 30)
        # available is now 0 -> second reservation raises
        with pytest.raises(ValueError):
            service.reserve_refund(conn, 1, 1.0, 30)
        # partial: two 60% requests cannot exceed 100%
        service.record_deposit(conn, 2, 1.0)
        a = service.reserve_refund(conn, 2, 0.6, 30)
        assert a["refund_sol"] == pytest.approx(0.6)
        b = service.reserve_refund(conn, 2, 0.6, 30)
        # second 60% applies to what is LEFT: 0.6 x 0.4 = 0.24
        assert b["refund_sol"] == pytest.approx(0.24)
        # total reserved (0.84) never exceeds the contribution (1.0)
        assert service.available_contribution(conn, 2) == pytest.approx(0.16)
        assert a["refund_sol"] + b["refund_sol"] <= 1.0

    def test_full_refund_zeroes(self, conn):
        service.record_deposit(conn, 1, 5.0)
        q = service.reserve_refund(conn, 1, 1.0, 30)
        service.commit_refund(conn, q["pending_id"])
        assert db.user_contribution(conn, 1) == 0.0
        with pytest.raises(ValueError):
            service.reserve_refund(conn, 1, 1.0, 30)

    def test_rejects_empty(self, conn):
        with pytest.raises(ValueError):
            service.reserve_refund(conn, 9, 1.0, 30)


class TestReconciliation:
    """Six categories; NEVER auto-corrects (report only)."""

    def test_reconcile_clean(self, conn):
        service.record_deposit(conn, 1, 2.0, "sigA:0")
        service.record_deposit(conn, 2, 1.0, "sigB:0")
        r = service.reconcile(conn, {"sigA:0": 2.0, "sigB:0": 1.0})
        assert r["missing_in_ledger"] == {}
        assert r["unverifiable_onchain"] == []
        assert r["amount_mismatch"] == {}
        assert r["duplicate_ledger"] == []
        assert r["unknown_refunds"] == []
        assert r["refund_sigs_unresolved"] == []

    def test_missing_in_ledger(self, conn):
        service.record_deposit(conn, 1, 2.0, "sigA:0")
        r = service.reconcile(conn, {"sigA:0": 2.0, "sigX:0": 0.5})
        assert r["missing_in_ledger"] == {"sigX:0": 0.5}

    def test_unverifiable_outside_window(self, conn):
        """Ledger sig not in the on-chain window: unverifiable, NOT missing."""
        service.record_deposit(conn, 1, 2.0, "sigA:0")
        service.record_deposit(conn, 2, 1.0, "sigGHOST:0")
        r = service.reconcile(conn, {"sigA:0": 2.0})
        assert r["missing_in_ledger"] == {}
        assert r["unverifiable_onchain"] == ["sigGHOST:0"]

    def test_amount_mismatch(self, conn):
        service.record_deposit(conn, 1, 2.0, "sigA:0")
        r = service.reconcile(conn, {"sigA:0": 1.5})
        assert r["amount_mismatch"] == {"sigA:0": (2.0, 1.5)}

    def test_duplicate_ledger(self, conn):
        service.record_deposit(conn, 1, 2.0, "sigA:0")
        service.record_deposit(conn, 2, 2.0, "sigA:0")
        r = service.reconcile(conn, {"sigA:0": 2.0})
        assert r["duplicate_ledger"] == ["sigA:0"]

    def test_unknown_refunds_listed(self, conn):
        service.record_deposit(conn, 1, 2.0)
        q = service.reserve_refund(conn, 1, 0.5, 30)
        service.mark_refund_unknown(conn, q["pending_id"])
        r = service.reconcile(conn, {})
        assert r["unknown_refunds"] == [q["pending_id"]]


class TestUnknownRefunds:
    def test_unknown_keeps_reservation(self, conn):
        service.record_deposit(conn, 1, 2.0)
        q = service.reserve_refund(conn, 1, 0.5, 30)
        service.mark_refund_unknown(conn, q["pending_id"])
        # reservation still held: available stays reduced
        assert service.available_contribution(conn, 1) == pytest.approx(1.0)
        assert db.user_contribution(conn, 1) == 2.0
        # a new 100% refund only sees what is left
        q2 = service.reserve_refund(conn, 1, 1.0, 30)
        assert q2["refund_sol"] == pytest.approx(1.0)

    def test_resolve_commits_when_landed(self, conn):
        import asyncio
        from curvebot import service as svc

        service.record_deposit(conn, 1, 2.0)
        db.set_wallet(conn, 1, "WALLETX")
        q = service.reserve_refund(conn, 1, 0.5, 30)
        net = q["net_sol"]
        service.mark_refund_unknown(conn, q["pending_id"])

        class FakeChain:
            async def transfers_to_wallet(self, wallet, max_signatures=25):
                return [{"signature": "sigR", "amount_sol": net}]

        resolutions = asyncio.run(svc.resolve_unknown_refunds(conn, FakeChain()))
        assert resolutions[0]["outcome"] == "committed"
        assert db.user_contribution(conn, 1) == pytest.approx(1.0)
        assert db.list_unknown_refunds(conn) == []

    def test_resolve_releases_after_grace(self, conn):
        import asyncio
        from datetime import datetime, timedelta, timezone

        from curvebot import service as svc

        service.record_deposit(conn, 1, 2.0)
        db.set_wallet(conn, 1, "WALLETX")
        q = service.reserve_refund(conn, 1, 0.5, 30)
        service.mark_refund_unknown(conn, q["pending_id"])
        # age the reservation beyond the grace period
        old = (datetime.now(timezone.utc) - timedelta(seconds=3600)
               ).isoformat()
        conn.execute("UPDATE pending_refunds SET created_at = ? WHERE id = ?",
                     (old, q["pending_id"]))

        class FakeChain:
            async def transfers_to_wallet(self, wallet, max_signatures=25):
                return []

        resolutions = asyncio.run(svc.resolve_unknown_refunds(conn, FakeChain()))
        assert resolutions[0]["outcome"] == "released"
        assert service.available_contribution(conn, 1) == pytest.approx(2.0)


class TestLaunchLock:
    def test_lock_acquire_release(self, conn):
        assert db.acquire_launch_lock(conn) is True
        assert db.launch_lock_held(conn) is True
        # second acquisition fails (atomic check-and-set)
        assert db.acquire_launch_lock(conn) is False
        db.release_launch_lock(conn)
        assert db.launch_lock_held(conn) is False
        assert db.acquire_launch_lock(conn) is True

    def test_lock_survives_reconnect(self, conn, tmp_path):
        """Persistent across connections: two processes cannot both launch."""
        assert db.acquire_launch_lock(conn) is True
        with db.connect(str(tmp_path / "test.db")) as conn2:
            assert db.launch_lock_held(conn2) is True
            assert db.acquire_launch_lock(conn2) is False


class TestSqliteAtomicity:
    def test_reserve_across_two_connections(self, conn, tmp_path):
        """Two connections racing: the second sees the first's reservation
        because check+insert are one IMMEDIATE transaction."""
        with db.connect(str(tmp_path / "test.db")) as conn2:
            service.record_deposit(conn, 1, 1.0)
            q1 = service.reserve_refund(conn, 1, 1.0, 30)
            assert q1["refund_sol"] == pytest.approx(1.0)
            with pytest.raises(ValueError):
                service.reserve_refund(conn2, 1, 1.0, 30)
