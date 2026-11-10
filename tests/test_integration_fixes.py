"""Regressions for the problems found while merging #4 (Solana validation)
and #5 (RC1/RC2 hardening): each test failed on one of the two branches."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from conftest import Recorder, make_call
from test_stress_launch import _run, world  # noqa: F401 - fixture reuse

from curvebot import db, service
from curvebot import handlers as h
from curvebot import launch as L
from curvebot.chain import Chain
from curvebot.monitor import DepositMonitor

WALLET = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"


# ---------------- refunds: reservations ----------------

def test_fresh_unknown_refund_is_not_released(conn):
    """#5: created_at is naive UTC (SQLite CURRENT_TIMESTAMP); comparing it
    with an aware datetime raised, the except branch released the
    reservation at once -> the user could refund again while the first
    transfer was still landing (double refund)."""
    service.record_deposit(conn, 1, 2.0)
    db.set_wallet(conn, 1, WALLET)
    q = service.reserve_refund(conn, 1, 1.0, 30)
    service.mark_refund_unknown(conn, q["pending_id"])

    class NothingYet:
        async def transfers_to_wallet(self, wallet, max_signatures=25):
            return []
    out = asyncio.run(service.resolve_unknown_refunds(conn, NothingYet()))
    assert out == []
    assert service.available_contribution(conn, 1) == pytest.approx(0.0)


def test_abandoned_quote_reserves_nothing(cfg, conn):
    """#5 reserved at quote time: a user who never confirmed kept that
    amount locked forever (and across restarts)."""
    service.record_deposit(conn, 1001, 4.0)
    db.set_wallet(conn, 1001, WALLET)
    asyncio.run(h.cb_refund_amt(make_call(1001, "refund:amt:100", Recorder()), cfg, conn))
    assert service.available_contribution(conn, 1001) == pytest.approx(4.0)
    assert conn.execute("SELECT COUNT(*) FROM pending_refunds").fetchone()[0] == 0


def test_restart_turns_inflight_reservation_into_unknown(conn):
    service.record_deposit(conn, 1, 2.0)
    q = service.reserve_refund(conn, 1, 0.5, 30)         # died before the outcome
    assert service.recover_after_restart(conn) == 1
    assert db.get_pending_refund(conn, q["pending_id"])["status"] == "unknown"
    assert service.available_contribution(conn, 1) == pytest.approx(1.0)  # still held


def test_refund_send_error_keeps_reservation(cfg, conn):
    from conftest import FakeChain
    service.record_deposit(conn, 1001, 2.0)
    db.set_wallet(conn, 1001, WALLET)
    chain = FakeChain()
    chain.fail_send = True
    rec = Recorder()

    async def flow():
        await h.cb_refund_amt(make_call(1001, "refund:amt:100", rec), cfg, conn)
        await h.cb_refund_go(make_call(1001, "refund:go", rec), cfg, conn, chain)
    asyncio.run(flow())
    assert "unknown" in rec.last
    assert len(db.list_unknown_refunds(conn)) == 1
    assert service.available_contribution(conn, 1001) == pytest.approx(0.0)


# ---------------- monitor ----------------

class _Scan:
    def __init__(self, transfers):
        self.transfers = transfers

    async def scan_incoming(self, max_pages=10, is_seen=None):
        fresh = [t for t in self.transfers if not (is_seen and is_seen(t["signature"]))]
        return fresh, sorted({t["signature"] for t in fresh})


def test_crash_between_credit_and_mark_never_double_credits(db_path, cfg, monkeypatch):
    """With #5's autocommit connection, a crash after record_deposit and
    before mark_sig credited the same deposit again on restart."""
    with db.connect(db_path) as c:
        db.set_wallet(c, 1, "WALLET_A")
    chain = _Scan([{"signature": "s1", "index": 0, "slot": 1,
                    "sender": "WALLET_A", "amount_sol": 1.0}])
    real_mark = db.mark_sig
    calls = {"n": 0}

    def flaky_mark(conn, sig):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("crash")
        real_mark(conn, sig)
    monkeypatch.setattr(db, "mark_sig", flaky_mark)
    mon = DepositMonitor(chain, cfg, lambda: db.connect(db_path))
    with pytest.raises(RuntimeError):
        asyncio.run(mon._poll_sol_deposits())
    asyncio.run(mon._poll_sol_deposits())
    asyncio.run(mon._poll_sol_deposits())
    with db.connect(db_path) as c:
        assert db.user_contribution(c, 1) == pytest.approx(1.0)
        assert c.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 1


def test_deposit_during_launch_is_not_credited(db_path, cfg):
    """After the launch snapshot a deposit can't get tokens: it must not
    be credited (it is reported by /reconcile for a manual refund)."""
    with db.connect(db_path) as c:
        db.set_wallet(c, 1, "WALLET_A")
        assert db.acquire_launch_lock(c)
    chain = _Scan([{"signature": "s9", "index": 0, "slot": 9,
                    "sender": "WALLET_A", "amount_sol": 2.0}])
    asyncio.run(DepositMonitor(chain, cfg, lambda: db.connect(db_path))._poll_sol_deposits())
    with db.connect(db_path) as c:
        assert db.user_contribution(c, 1) == 0


def test_scan_skips_seen_history_without_refetching():
    """#5 re-read the whole treasury history (up to 10 pages x 1000 tx,
    one get_transaction each) on EVERY 5-second poll."""
    pages = [[SimpleNamespace(signature=f"p{p}s{i}", slot=1000 - p * 10 - i, err=None)
              for i in range(10)] for p in range(3)]
    calls = {"sigs": 0, "tx": 0}

    class Client:
        async def get_signatures_for_address(self, pk, before=None, **kw):
            calls["sigs"] += 1
            if before is None:
                return SimpleNamespace(value=pages[0])
            idx = next(i for i, pg in enumerate(pages) if pg[-1].signature == before)
            return SimpleNamespace(value=pages[idx + 1] if idx + 1 < len(pages) else [])

        async def get_transaction(self, sig, **kw):
            calls["tx"] += 1
            return SimpleNamespace(value=None)
    c = Chain.__new__(Chain)
    c.keypair = __import__("solders.keypair", fromlist=["Keypair"]).Keypair()
    c.client = Client()
    seen = {s.signature for pg in pages[1:] for s in pg}      # only page 0 is new
    asyncio.run(c.scan_incoming(is_seen=lambda s: s in seen))
    assert calls["tx"] == 10
    assert calls["sigs"] <= 2


# ---------------- launch lock ----------------

def test_launch_lock_released_on_failure_kept_on_unknown(world, cfg, conn):  # noqa: F811
    world.seed(3, 1.0)
    world.state["status"] = "failed"
    with pytest.raises(RuntimeError):
        _run(world, cfg, conn)
    assert not db.launch_lock_held(conn)                     # retry allowed
    world.state["status"] = "pending"
    with pytest.raises(L.LaunchOutcomeUnknown):
        _run(world, cfg, conn)
    assert db.launch_lock_held(conn)                         # never retry blindly
    assert db.get_meta(conn, "state") == "launch_unknown"
    with pytest.raises(RuntimeError, match="lock"):
        _run(world, cfg, conn)


@pytest.mark.parametrize("on_chain,age,expected,state,locked", [
    (True, 10, "landed", "launched", True),
    (False, 10, "unknown", "launch_unknown", True),
    (False, 400, "failed", "", False),
])
def test_resolve_unknown_launch_reads_the_chain(world, cfg, conn, on_chain, age,  # noqa: F811
                                                expected, state, locked):
    import time
    world.seed(3, 1.0)
    world.state["status"] = "pending"
    with pytest.raises(L.LaunchOutcomeUnknown):
        _run(world, cfg, conn)
    mint = db.get_meta(conn, "pending_mint")
    assert mint

    async def account(pk, commitment=None):
        assert str(pk) == mint
        return SimpleNamespace(value=object() if on_chain else None)
    world.chain.client.get_account_info = account
    out = asyncio.run(L.resolve_unknown_launch(world.chain, conn, now=time.time() + age))
    assert out == expected
    assert (db.get_meta(conn, "state") or "") == state
    assert db.launch_lock_held(conn) is locked
    if expected == "landed":            # same bookkeeping as the normal path
        assert db.get_meta(conn, "mint") == mint
        row = conn.execute("SELECT mint, dev_buy_sol, tokens_distributed FROM launches").fetchone()
        assert row["mint"] == mint and row["dev_buy_sol"] > 0 and row["tokens_distributed"] > 0
    else:
        assert conn.execute("SELECT COUNT(*) FROM launches").fetchone()[0] == 0


def test_reconcile_command_runs_against_the_real_chain_api(cfg, conn):
    """Merge #6 left /reconcile calling incoming_sol_transfers(max_signatures=50),
    an argument that no longer existed: the command crashed on every call."""
    from conftest import ADMIN, make_message
    from solders.keypair import Keypair
    seen_kw = {}

    class Client:
        async def get_signatures_for_address(self, pk, **kw):
            seen_kw.update(kw)
            return SimpleNamespace(value=[])
    chain = Chain.__new__(Chain)
    chain.keypair, chain.client = Keypair(), Client()
    rec = Recorder()
    asyncio.run(h.cmd_reconcile(make_message(ADMIN, rec), cfg, conn, chain))
    assert "Nessuna discrepanza" in rec.last
    assert seen_kw["limit"] == 50                 # the advertised 50-tx window


def test_unknown_refund_never_matches_an_already_booked_transfer(conn):
    """Two refunds of the same amount: the first landed and is booked, the
    second never left. Matching by amount alone booked the second against
    the first one's signature: the user lost that contribution."""
    db.set_wallet(conn, 1, WALLET)
    service.record_deposit(conn, 1, 4.0)
    q1 = service.reserve_refund(conn, 1, 0.25, 30)
    service.commit_refund(conn, q1["pending_id"], signature="OLD_SIG")
    service.record_deposit(conn, 1, 1.0)
    q2 = service.reserve_refund(conn, 1, 0.25, 30)
    service.mark_refund_unknown(conn, q2["pending_id"])

    class OnlyTheOldOne:
        async def transfers_to_wallet(self, wallet, max_signatures=25):
            return [{"signature": "OLD_SIG", "amount_sol": q1["net_sol"]}]
    assert asyncio.run(service.resolve_unknown_refunds(conn, OnlyTheOldOne())) == []
    assert db.user_contribution(conn, 1) == pytest.approx(4.0)
    assert len(db.list_unknown_refunds(conn)) == 1          # still held


def test_two_unknown_refunds_need_two_distinct_transfers(conn):
    db.set_wallet(conn, 1, WALLET)
    service.record_deposit(conn, 1, 4.0)
    qa = service.reserve_refund(conn, 1, 0.25, 30)
    service.mark_refund_unknown(conn, qa["pending_id"])
    service.record_deposit(conn, 1, 1.0)
    qb = service.reserve_refund(conn, 1, 0.25, 30)
    service.mark_refund_unknown(conn, qb["pending_id"])
    assert qa["net_sol"] == pytest.approx(qb["net_sol"])

    class One:
        async def transfers_to_wallet(self, wallet, max_signatures=25):
            return [{"signature": "S1", "amount_sol": qa["net_sol"]}]
    out = asyncio.run(service.resolve_unknown_refunds(conn, One()))
    assert [r["outcome"] for r in out] == ["committed"]
    assert len(db.list_unknown_refunds(conn)) == 1
