"""Deposit detection against REAL solders RPC response objects, plus the
monitor polling window and idempotency. All offline.

The code builds its own SOL-transfer tx the same way `send_sol` does, wraps
it in a genuine GetTransactionResp decoded from JSON (exactly the shape
solana-py returns), and feeds it through the parsing path.
"""
from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

import base58
import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction

from curvebot import db
from curvebot.chain import Chain
from curvebot.monitor import DepositMonitor


def _tx(sender: Keypair, to: Pubkey, lamports: int) -> Transaction:
    ix = transfer(TransferParams(from_pubkey=sender.pubkey(),
                                 to_pubkey=to, lamports=lamports))
    return Transaction.new_signed_with_payer([ix], sender.pubkey(),
                                             [sender], Hash.default())


def _resp(tx: Transaction, encoding: str):
    """Build a GetTransactionResp exactly as solana-py returns it."""
    from solders.rpc.responses import GetTransactionResp
    msg = tx.message
    if encoding == "json":
        inner = {"signatures": [str(tx.signatures[0])], "message": {
            "accountKeys": [str(k) for k in msg.account_keys],
            "header": {"numRequiredSignatures": msg.header.num_required_signatures,
                       "numReadonlySignedAccounts": msg.header.num_readonly_signed_accounts,
                       "numReadonlyUnsignedAccounts": msg.header.num_readonly_unsigned_accounts},
            "recentBlockhash": str(msg.recent_blockhash),
            "instructions": [{"programIdIndex": ci.program_id_index,
                              "accounts": list(ci.accounts),
                              "data": base58.b58encode(bytes(ci.data)).decode(),
                              "stackHeight": None} for ci in msg.instructions]}}
    else:
        inner = [base64.b64encode(bytes(tx)).decode(), "base64"]
    n = len(msg.account_keys)
    doc = {"jsonrpc": "2.0", "id": 1, "result": {
        "slot": 1, "blockTime": 1, "transaction": inner,
        "meta": {"err": None, "status": {"Ok": None}, "fee": 5000,
                 "preBalances": [0] * n, "postBalances": [0] * n,
                 "innerInstructions": [], "logMessages": [],
                 "preTokenBalances": [], "postTokenBalances": [], "rewards": [],
                 "loadedAddresses": {"writable": [], "readonly": []},
                 "computeUnitsConsumed": 150}}}
    return GetTransactionResp.from_json(json.dumps(doc))


def _chain(kp: Keypair) -> Chain:
    c = Chain.__new__(Chain)
    c.keypair = kp
    return c


@pytest.mark.parametrize("encoding", ["json", "base64"])
def test_parse_real_rpc_object(encoding):
    treasury, sender = Keypair(), Keypair()
    tx = _tx(sender, treasury.pubkey(), 500_000_000)
    got = _chain(treasury)._find_incoming_transfers(_resp(tx, encoding).value)
    assert got == [{"sender": str(sender.pubkey()), "amount_sol": 0.5}]


def _fixed_path(resp):
    """The shape the parser expects, i.e. what a fix should pass in."""
    return SimpleNamespace(transaction=resp.value.transaction.transaction)


def test_parser_logic_ok_when_given_the_inner_tx():
    treasury, sender = Keypair(), Keypair()
    tx = _tx(sender, treasury.pubkey(), 250_000_000)
    got = _chain(treasury)._find_incoming_transfers(_fixed_path(_resp(tx, "base64")))
    assert got == [{"sender": str(sender.pubkey()), "amount_sol": 0.25}]


def test_outgoing_transfer_is_not_credited():
    """A refund FROM the treasury must not be read as a deposit."""
    treasury, other = Keypair(), Keypair()
    tx = _tx(treasury, other.pubkey(), 100_000_000)
    assert _chain(treasury)._find_incoming_transfers(_fixed_path(_resp(tx, "base64"))) == []


def test_rpc_fetch_requests_base64_and_v0():
    seen = {}

    class Client:
        async def get_signatures_for_address(self, pk, limit=None, **kw):
            return SimpleNamespace(value=[SimpleNamespace(signature="s", slot=1, err=None)])

        async def get_transaction(self, sig, **kw):
            seen.update(kw)
            return SimpleNamespace(value=None)
    c = _chain(Keypair())
    c.client = Client()
    asyncio.run(c.incoming_sol_transfers())
    assert seen["encoding"] == "base64"
    assert seen["max_supported_transaction_version"] == 0
    assert str(seen.get("commitment", "")).lower().endswith("finalized")


def test_only_system_program_instructions_count():
    """Regression for the program-id check: same data layout, other program."""
    from solders.instruction import AccountMeta, Instruction
    treasury, sender = Keypair(), Keypair()
    data = b"\x02\x00\x00\x00" + (10**9).to_bytes(8, "little")
    ix = Instruction(Keypair().pubkey(), data,
                     [AccountMeta(sender.pubkey(), True, True),
                      AccountMeta(treasury.pubkey(), False, True)])
    tx = Transaction.new_signed_with_payer([ix], sender.pubkey(), [sender], Hash.default())
    assert _chain(treasury)._find_incoming_transfers(_resp(tx, "base64").value) == []


# ---------------- monitor loop: window + idempotency ----------------

class FakeChainRPC:
    def __init__(self, transfers):
        self._transfers = transfers

    async def scan_incoming(self, max_pages=10, is_seen=None):
        fresh = [t for t in self._transfers if not (is_seen and is_seen(t["signature"]))]
        return fresh, sorted({t["signature"] for t in fresh})


def _run_monitor(db_path, cfg, transfers, wallets, pre_last=None):
    for uid, w in wallets.items():
        with db.connect(db_path) as c:
            db.set_wallet(c, uid, w)
    mon = DepositMonitor(FakeChainRPC(transfers), cfg, lambda: db.connect(db_path))
    if pre_last is not None:
        mon.last_signature = pre_last
    asyncio.run(mon._poll_sol_deposits())
    return mon


def test_monitor_credits_known_wallet(db_path, cfg):
    t = [{"signature": "s1", "slot": 1, "sender": "WALLET_A", "amount_sol": 1.0}]
    _run_monitor(db_path, cfg, t, {1: "WALLET_A"})
    with db.connect(db_path) as c:
        assert db.user_contribution(c, 1) == pytest.approx(1.0)
        assert db.sig_seen(c, "s1:0")


def test_monitor_ignores_unknown_and_below_min(db_path, cfg):
    t = [{"signature": "s1", "slot": 1, "sender": "STRANGER", "amount_sol": 5.0},
         {"signature": "s2", "slot": 1, "sender": "WALLET_A", "amount_sol": 0.0001}]
    _run_monitor(db_path, cfg, t, {1: "WALLET_A"})
    with db.connect(db_path) as c:
        assert db.user_contribution(c, 1) == 0
        # below-min sig is still consumed (won't be retried), unknown too
        assert db.sig_seen(c, "s1:0") and db.sig_seen(c, "s2:0")


def test_monitor_idempotent_across_polls(db_path, cfg):
    t = [{"signature": "s1", "slot": 1, "sender": "WALLET_A", "amount_sol": 2.0}]
    _run_monitor(db_path, cfg, t, {1: "WALLET_A"})
    # second poll, same history, fresh monitor (restart): sig already seen
    m2 = DepositMonitor(FakeChainRPC(t), cfg, lambda: db.connect(db_path))
    asyncio.run(m2._poll_sol_deposits())
    with db.connect(db_path) as c:
        assert db.user_contribution(c, 1) == pytest.approx(2.0)   # not doubled


def test_monitor_window_break_does_not_skip_older_new_sig(db_path, cfg):
    # newest-first history; s_new is unseen (index 0), s_old already seen.
    # last_signature points at s_old -> loop breaks on reaching it, which is
    # correct. But a sig BELOW s_old in the SAME batch, newly appearing, is
    # never processed.
    t = [{"signature": "s_new", "slot": 3, "sender": "WALLET_A", "amount_sol": 1.0},
         {"signature": "s_seen", "slot": 2, "sender": "WALLET_A", "amount_sol": 1.0},
         {"signature": "s_late", "slot": 1, "sender": "WALLET_A", "amount_sol": 1.0}]
    with db.connect(db_path) as c:
        db.set_wallet(c, 1, "WALLET_A")
        db.mark_sig(c, "s_seen:0")
    mon = DepositMonitor(FakeChainRPC(t), cfg, lambda: db.connect(db_path))
    mon.last_signature = "s_seen"
    asyncio.run(mon._poll_sol_deposits())
    with db.connect(db_path) as c:
        # s_new (1.0) credited; s_late (1.0) should also be, but the break skips it
        assert db.user_contribution(c, 1) == pytest.approx(2.0)
