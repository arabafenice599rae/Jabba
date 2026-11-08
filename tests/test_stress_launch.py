"""run_launch end-to-end with simulated RPC / Pinata / PumpPortal / Jito.
Real Chain object, real tx building and signing; only the network is fake."""
from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

import pytest
from solders.hash import Hash
from solders.keypair import Keypair

from conftest import finding
from curvebot import db, launch as L, pump_curve as pc, service
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
        return SimpleNamespace(value=SimpleNamespace(
            data=[base64.b64encode(self.global_raw).decode(), "base64"]))

    async def get_minimum_balance_for_rent_exemption(self, n):
        return SimpleNamespace(value=L.TOKEN_ACCOUNT_RENT_LAMPORTS)

    async def get_balance(self, pk):
        return SimpleNamespace(value=self.lamports)

    async def get_latest_blockhash(self):
        return SimpleNamespace(value=SimpleNamespace(blockhash=Hash.default()))


@pytest.fixture
def world(monkeypatch, conn):
    state = {"bundle": None, "status": "confirmed", "global": _global_account(),
             "lamports": 0}
    c = Chain.__new__(Chain)
    c.keypair = Keypair()
    c.client = RPC(0, state["global"])

    async def fake_pinata(http, cfg):
        return "https://ipfs.io/ipfs/meta"

    class FakePortal:
        def __init__(self, http):
            pass

        async def create_with_devbuy(self, *a, **k):
            return "fake-b58"

    async def send_bundle(http, txs):
        state["bundle"] = txs
        return "bundle-1"

    async def wait_bundle(http, bid, **k):
        return state["status"]

    async def tips(http):
        return list(JITO_TIP_ACCOUNTS)

    monkeypatch.setattr(L, "upload_pinata_metadata", fake_pinata)
    monkeypatch.setattr(L, "PumpPortal", FakePortal)
    monkeypatch.setattr(L, "wait_bundle", wait_bundle)
    c.sign_b58 = lambda b58, extra_signers=None: b"create-tx"
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
    assert bundle[0] == b"create-tx"
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


def test_launch_aborts_when_treasury_short(world, cfg, conn):
    world.seed(3, 1.0)
    world.chain.client.lamports = int(2.0 * 1e9)      # less than dev buy + costs
    with pytest.raises(RuntimeError, match="treasury insufficiente"):
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


def test_preflight_includes_create_rent(world, cfg, conn):
    cfg.team_keep_percent = 0.0
    world.seed(5, 1.0, extra_lamports=40_000_000)    # covers our estimate only
    with pytest.raises(RuntimeError, match="treasury insufficiente"):
        _run(world, cfg, conn)


def test_launch_rejects_raise_beyond_curve_capacity(world, cfg, conn):
    world.seed(20, 6.0)                               # 120 SOL
    with pytest.raises(RuntimeError):
        _run(world, cfg, conn)
    assert world.state["bundle"] is None


def test_safety_margin_vs_slippage(world, cfg, conn):
    """Documents the margin: SAFETY_BPS=10 (0.1%) is all that protects the
    distribution if the real dev buy returns fewer tokens than computed,
    while LAUNCH_SLIPPAGE allows 10%. Report the ratio."""
    world.seed(4, 2.0)
    rep = _run(world, cfg, conn)
    margin = 1 - rep["distributed"] / rep["tokens_bought"]
    assert margin == pytest.approx(cfg.safety_bps / 10_000, abs=1e-6)
    assert margin < cfg.launch_slippage / 100     # margin << allowed slippage


def test_monitor_stops_after_launch(db_path, cfg):
    with db.connect(db_path) as c:
        db.set_wallet(c, 1, "WALLET_A")
        db.set_meta(c, "state", "launched")

    class Fake:
        async def incoming_sol_transfers(self, max_signatures=25):
            return [{"signature": "late", "slot": 9, "sender": "WALLET_A",
                     "amount_sol": 3.0}]
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
