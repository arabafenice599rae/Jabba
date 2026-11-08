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
    def test_reconcile_clean(self, conn):
        service.record_deposit(conn, 1, 2.0, "sigA:0")
        service.record_deposit(conn, 2, 1.0, "sigB:0")
        r = service.reconcile(conn, {"sigA:0": 2.0, "sigB:0": 1.0})
        assert r["missing_in_ledger"] == {}
        assert r["missing_onchain"] == []

    def test_reconcile_missing_in_ledger(self, conn):
        service.record_deposit(conn, 1, 2.0, "sigA:0")
        r = service.reconcile(conn, {"sigA:0": 2.0, "sigX:0": 0.5})
        assert r["missing_in_ledger"] == {"sigX:0": 0.5}
        assert r["missing_onchain"] == []

    def test_reconcile_missing_onchain(self, conn):
        service.record_deposit(conn, 1, 2.0, "sigA:0")
        service.record_deposit(conn, 2, 1.0, "sigGHOST:0")
        r = service.reconcile(conn, {"sigA:0": 2.0})
        assert r["missing_in_ledger"] == {}
        assert r["missing_onchain"] == ["sigGHOST:0"]
