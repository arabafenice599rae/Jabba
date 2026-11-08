"""Ledger: stateful model-based fuzzing + SQLite persistence across
the two connections main.py actually uses (handlers vs monitor)."""
from __future__ import annotations

import sqlite3
from fractions import Fraction

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from conftest import finding
from curvebot import db, service

UIDS = st.sampled_from([1, 2, 3, 4, 5])
PCTS = st.sampled_from([25, 50, 75, 100])


class LedgerMachine(RuleBasedStateMachine):
    """Model: per-user net contribution in integer lamports. Real system
    uses floats in SQLite; we check it never drifts from the model."""

    def __init__(self):
        super().__init__()
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)
        self.model: dict[int, Fraction] = {}
        self.fees = 0

    @rule(uid=UIDS, lamports=st.integers(10_000_000, 50 * 10**9))
    def deposit(self, uid, lamports):
        service.record_deposit(self.conn, uid, lamports / 1e9)
        self.model[uid] = self.model.get(uid, Fraction(0)) + Fraction(lamports / 1e9)

    @rule(uid=UIDS, pct=PCTS)
    def refund(self, uid, pct):
        c = db.user_contribution(self.conn, uid)
        try:
            q = service.refund_quote(self.conn, uid, pct / 100, 30)
        except ValueError:
            assert c <= 1e-9 or self.model.get(uid, 0) == 0
            return
        assert q["refund_sol"] <= c + 1e-12
        assert q["net_sol"] + q["fee_sol"] == pytest.approx(q["refund_sol"])
        assert q["fee_sol"] == pytest.approx(q["refund_sol"] * 0.003)
        service.confirm_refund(self.conn, uid, q["refund_sol"], q["fee_sol"])
        self.model[uid] = self.model.get(uid, Fraction(0)) - Fraction(q["refund_sol"])
        self.fees += q["fee_sol"]

    @invariant()
    def contributions_match_model(self):
        sh = service.shares(self.conn)
        for uid, exact in self.model.items():
            assert abs(Fraction(sh.get(uid, 0)) - exact) < Fraction(1, 10**9)  # < 1 lamport
            assert sh.get(uid, 0) > -1e-9                    # never negative

    @invariant()
    def total_is_sum(self):
        assert db.total_raised(self.conn) == pytest.approx(
            sum(service.shares(self.conn).values()), abs=1e-9)


TestLedgerMachine = LedgerMachine.TestCase
TestLedgerMachine.settings = settings(max_examples=400, stateful_step_count=60,
                                      deadline=None)


def test_full_refund_leaves_zero_but_user_still_counted(conn):
    """After a 100% refund the user stays in shares() with 0 and is
    still counted as 'participant' in /info."""
    service.record_deposit(conn, 1, 0.1)
    service.record_deposit(conn, 1, 0.2)
    q = service.refund_quote(conn, 1, 1.0, 30)
    service.confirm_refund(conn, 1, q["refund_sol"], q["fee_sol"])
    assert abs(service.shares(conn)[1]) < 1e-12
    assert 1 in service.shares(conn)


def test_float_residue_after_partial_refunds(conn):
    """25% x N refunds on awkward amounts: residue must stay tiny and
    must not turn into a refundable 'dust' that the quote accepts."""
    for amt in (0.1, 0.2, 0.3, 0.7):
        service.record_deposit(conn, 1, amt)
    for _ in range(200):
        try:
            q = service.refund_quote(conn, 1, 0.25, 30)
        except ValueError:
            break
        service.confirm_refund(conn, 1, q["refund_sol"], q["fee_sol"])
    left = db.user_contribution(conn, 1)
    assert left >= 0
    # documents: quotes keep being accepted for sub-lamport amounts
    if left > 0:
        q = service.refund_quote(conn, 1, 1.0, 30)
        assert int(round(q["net_sol"] * 1e9)) == 0  # a 0-lamport transfer would be sent


@finding("refund_quote accepts sub-lamport refunds (net rounds to 0 lamports)")
def test_refund_quote_rejects_sub_lamport(conn):
    service.record_deposit(conn, 1, 1e-10)
    with pytest.raises(ValueError):
        service.refund_quote(conn, 1, 1.0, 30)


@pytest.mark.parametrize("fraction", [1.5, 2.0])
@finding("refund_quote does not clamp fraction to [0,1] (docstring says it must)")
def test_refund_fraction_above_one_rejected(conn, fraction):
    service.record_deposit(conn, 1, 1.0)
    with pytest.raises(ValueError):
        service.refund_quote(conn, 1, fraction, 30)


def test_refund_fee_bps_over_100pct_rejected(conn):
    service.record_deposit(conn, 1, 1.0)
    with pytest.raises(ValueError):
        service.refund_quote(conn, 1, 0.5, 20_000)


# ---------------- persistence across connections (main.py layout) ----------------

def test_handler_writes_survive_restart(db_path):
    h = db.open_conn(db_path)
    db.set_wallet(h, 1, "W" * 44)
    service.confirm_refund(h, 1, 1.0, 0.003)
    db.set_meta(h, "state", "launched")
    h.close()                                   # crash / restart
    with db.connect(db_path) as c:
        assert db.get_wallet(c, 1) == "W" * 44
        assert db.get_meta(c, "state") == "launched"
        assert c.execute("SELECT COUNT(*) FROM ledger WHERE kind='refund'").fetchone()[0] == 1


def test_monitor_sees_wallet_set_by_handler(db_path):
    h = db.open_conn(db_path)
    db.set_wallet(h, 1, "W" * 44)
    with db.connect(db_path) as m:
        assert db.get_wallet(m, 1) == "W" * 44
    h.close()


def test_monitor_can_write_while_handler_active(db_path):
    h = db.open_conn(db_path)
    db.set_wallet(h, 1, "W" * 44)
    m = sqlite3.connect(db_path, timeout=0.3)
    m.execute("INSERT INTO ledger (telegram_id, kind, amount_sol) VALUES (1,'deposit',1)")
    m.commit()
    h.close()
