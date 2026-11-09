"""run_launch end-to-end with simulated RPC / Pinata / Jito.
Real Chain object, real tx building and signing; only the network is fake."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from solders.hash import Hash
from solders.keypair import Keypair

from curvebot import db, service
from curvebot import launch as L
from curvebot import pump_curve as pc
from curvebot import pump_ix as P
from curvebot.chain import JITO_TIP_ACCOUNTS, Chain
from curvebot.monitor import DepositMonitor


def _global_account(override=None) -> bytes:
    raw = bytearray(200)
    vals = {73: pc.VTR0, 81: pc.VSR0, 89: pc.RTR0, 97: pc.SUPPLY, 105: pc.PROTOCOL_FEE_BPS}
    vals.update(override or {})
    for off, v in vals.items():
        raw[off:off + 8] = int(v).to_bytes(8, "little")
    return bytes(raw)


class RPC:
    def __init__(self, lamports, global_raw):
        self.lamports, self.global_raw = lamports, global_raw

    async def get_account_info(self, pk):
        # solana-py shape: Account.data is already-decoded bytes
        return SimpleNamespace(value=SimpleNamespace(data=self.global_raw))

    async def get_minimum_balance_for_rent_exemption(self, n):
        return SimpleNamespace(value=L.TOKEN_ACCOUNT_RENT_LAMPORTS)

    async def get_balance(self, pk):
        return SimpleNamespace(value=self.lamports)

    async def get_latest_blockhash(self):
        return SimpleNamespace(value=SimpleNamespace(blockhash=Hash.default()))


def _decode_create(raw: bytes):
    """(program, data) of each instruction of the create tx."""
    from solders.transaction import VersionedTransaction
    tx = VersionedTransaction.from_bytes(raw)
    keys = tx.message.account_keys
    return tx, [(str(keys[ix.program_id_index]), bytes(ix.data)) for ix in tx.message.instructions]


@pytest.fixture
def world(monkeypatch, conn):
    state = {"bundle": None, "status": "confirmed", "global": _global_account(),
             "lamports": 0}
    c = Chain.__new__(Chain)
    c.keypair = Keypair()
    c.client = RPC(0, state["global"])

    async def fake_pinata(http, cfg):
        return "https://ipfs.io/ipfs/meta"

    async def send_bundle(http, txs):
        state["bundle"] = txs
        return "bundle-1"

    async def wait_bundle(http, bid, **k):
        return state["status"]

    async def tips(http):
        return list(JITO_TIP_ACCOUNTS)

    monkeypatch.setattr(L, "upload_pinata_metadata", fake_pinata)
    monkeypatch.setattr(L, "wait_bundle", wait_bundle)
    c.send_bundle = send_bundle
    c.fetch_tip_accounts = tips

    async def bal():
        return c.client.lamports / 1e9
    c.balance_sol = bal

    def seed(n, sol_each, extra_lamports=0):
        for i in range(n):
            uid = 5000 + i
            db.set_wallet(conn, uid, str(Keypair().pubkey()))
            service.record_deposit(conn, uid, sol_each)
        c.client.lamports = int(n * sol_each * 1e9) + extra_lamports
    return SimpleNamespace(chain=c, state=state, seed=seed)


def _run(world, cfg, conn):
    return asyncio.run(L.run_launch(world.chain, cfg, conn, http=None))


@pytest.mark.parametrize("n", [1, 9, 10, 27, 36])
def test_launch_success_bundle_shape(world, cfg, conn, n):
    world.seed(n, 0.5)
    rep = _run(world, cfg, conn)
    bundle = world.state["bundle"]
    tx, ixs = _decode_create(bundle[0])
    assert len(bundle[0]) <= L.TX_SIZE_LIMIT
    assert len(tx.signatures) == 2                      # treasury + mint
    assert [d[:8].hex() for p, d in ixs if p == P.PUMP_PROGRAM] == \
        [P.CREATE_V2.hex(), P.BUY_V3.hex()]               # no PumpPortal router
    buy = next(d for p, d in ixs if p == P.PUMP_PROGRAM and d[:8] == P.BUY_V3)
    amount = int.from_bytes(buy[8:16], "little")
    assert amount == round(rep["tokens_bought"] * 10**6)  # exact buy...
    dust = round(rep["dust_tokens"] * 10**6)
    assert round(rep["distributed"] * 10**6) + dust == amount  # ...all distributed
    assert dust < n                                       # floor rounding only
    assert len(bundle) == 1 + -(-n // L.CHUNK_SIZE) <= 5
    assert all(len(tx) <= L.TX_SIZE_LIMIT for tx in bundle[1:])
    assert rep["wallets"] == n
    assert rep["distributed"] <= rep["tokens_bought"]
    assert db.get_meta(conn, "state") == "launched"


def test_launch_aborts_on_curve_param_change(world, cfg, conn):
    world.seed(3, 1.0)
    world.chain.client.global_raw = _global_account({105: 100})
    with pytest.raises(RuntimeError, match="parametri curva"):
        _run(world, cfg, conn)
    assert world.state["bundle"] is None


def test_launch_aborts_when_treasury_far_too_short(world, cfg, conn):
    world.seed(3, 1.0)
    world.chain.client.lamports = int(0.4 * 1e9)      # can't fund half the raise
    with pytest.raises(RuntimeError, match="troppo piccola"):
        _run(world, cfg, conn)
    assert world.state["bundle"] is None


def test_launch_failed_bundle_keeps_presale_open(world, cfg, conn):
    world.seed(3, 1.0)
    world.state["status"] = "failed"
    with pytest.raises(RuntimeError, match="fallito"):
        _run(world, cfg, conn)
    assert db.get_meta(conn, "state") is None


def test_launch_unknown_outcome_blocks_refunds(world, cfg, conn):
    world.seed(3, 1.0)
    world.state["status"] = "pending"
    with pytest.raises(RuntimeError, match="SCONOSCIUTO"):
        _run(world, cfg, conn)
    assert db.get_meta(conn, "state") not in (None, "")


def test_zero_team_keep_lowers_the_dev_buy_to_fit(world, cfg, conn):
    """TEAM_KEEP_PERCENT=0 used to be refused; the dynamic dev buy now
    takes exactly what is left after every cost."""
    cfg.team_keep_percent = 0.0
    world.seed(5, 1.0)
    rep = _run(world, cfg, conn)
    assert 4.8 < rep["dev_buy_sol"] < 5.0
    assert 0 <= rep["team_sol_left"] < 0.001


def test_launch_rejects_raise_beyond_curve_capacity(world, cfg, conn):
    world.seed(20, 6.0)                               # 120 SOL
    with pytest.raises(RuntimeError):
        _run(world, cfg, conn)
    assert world.state["bundle"] is None


def test_small_test_launch_now_starts(world, cfg, conn):
    """0.1 SOL from 3 wallets: refused with the fixed 90% dev buy."""
    world.seed(3, 0.1 / 3)
    rep = _run(world, cfg, conn)
    assert 0.07 < rep["dev_buy_sol"] < 0.09
    assert rep["tokens_bought"] - rep["distributed"] < 1e-5  # SAFETY_BPS=0: all to users


def test_monitor_stops_after_launch(db_path, cfg):
    with db.connect(db_path) as c:
        db.set_wallet(c, 1, "WALLET_A")
        db.set_meta(c, "state", "launched")

    class Fake:
        async def scan_incoming(self, max_pages=10, is_seen=None):
            return [{"signature": "late", "slot": 9, "sender": "WALLET_A",
                     "amount_sol": 3.0}], ["late"]
    asyncio.run(DepositMonitor(Fake(), cfg, lambda: db.connect(db_path))._poll_sol_deposits())
    with db.connect(db_path) as c:
        assert db.user_contribution(c, 1) == 0


def test_incoming_scan_paginates(cfg):
    sigs = [SimpleNamespace(signature=f"s{i}", slot=100 - i, err=None) for i in range(40)]

    class Client:
        async def get_signatures_for_address(self, pk, limit=None, **kw):
            return SimpleNamespace(value=sigs[:limit])

        async def get_transaction(self, sig, **kw):
            return SimpleNamespace(value=object())
    c = Chain.__new__(Chain)
    c.keypair = Keypair()
    c.client = Client()
    c._find_incoming_transfers = lambda v: [{"sender": "A", "amount_sol": 1.0}]
    out = asyncio.run(c.incoming_sol_transfers())
    assert len(out) == 40
