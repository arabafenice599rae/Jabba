"""Atomic launch on pump.fun via a Jito bundle: create+devbuy (ONE tx) +
distribution chunks, all in the same slot. If any tx fails, everything
reverts — the treasury never holds tokens in any confirmed state.

Verified facts (2026-11):
- PumpPortal creation goes through POST /api/trade-local with
  action="create"; the dev buy is INCLUDED in the create tx; metadata
  must be uploaded to IPFS (Pinata) beforehand.
- pump.fun global account: VTR 1.073B, VSR 30 SOL, fee 95 bps protocol
  (+30 bps creator), 6 decimals, supply 1B. Verified at launch time.

Distribution math works in integer BASE UNITS (10**6) so the invariant
    sum(user_tokens) + team + dust <= tokens_bought
holds with deterministic floor rounding; dust stays in the treasury.

Atomicity limits (measured): 92 bytes per recipient, tx limit 1232 B
-> 9 recipients per tx (incl. room for the Jito tip), 4 distribution
txs per bundle (1 slot goes to create+devbuy) -> MAX 36 participants.
Multiple bundles are NOT atomic across each other: beyond 36 users the
launch is REJECTED, not silently chained.
"""

from __future__ import annotations

import logging

import aiohttp

from . import db, service
from .pump_curve import DECIMALS, params_match, tokens_out_for_sol, verify_params

log = logging.getLogger("launch")

PUMPPORTAL_BASE = "https://pumpportal.fun/api"
GLOBAL_ACCOUNT = "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"
TX_SIZE_LIMIT = 1232
BYTES_PER_RECIPIENT = 92          # measured: ATA create + transfer_checked
TIP_OVERHEAD = 64                 # measured: tip transfer ix + account key
BASE_TX_OVERHEAD = 294            # measured: header + shared accounts
CHUNK_SIZE = 9                    # (1232 - 294 - 64) // 92 == 9
MAX_DIST_TXS = 4                  # bundle = create + 4 chunks = 5 txs
MAX_PARTICIPANTS = CHUNK_SIZE * MAX_DIST_TXS


def compute_distribution(weights: dict[int, float], tokens_base: int,
                        team_bps: int, safety_bps: int = 0
                        ) -> tuple[list[tuple[int, int]], int, int]:
    """Split tokens_base (base units) proportionally to weights.

    Returns ([(uid, base_amount)], team_base, dust_base).
    Invariant: sum(shares) + team + dust == distributable <= tokens_base.
    Floor rounding; the dust stays in the treasury.
    """
    if not weights or tokens_base <= 0:
        return [], 0, 0
    total_w = sum(weights.values())
    if total_w <= 0:
        return [], 0, 0
    team = tokens_base * team_bps // 10_000
    distributable = tokens_base * (10_000 - team_bps - safety_bps) // 10_000
    out = []
    assigned = 0
    for uid in sorted(weights):
        share = distributable * weights[uid] // total_w
        out.append((uid, share))
        assigned += share
    dust = distributable - assigned
    return out, team, dust


def plan_chunks(recipients: list) -> list[list]:
    """Split recipients into chunks that fit the tx size limit.
    Raises ValueError if the count exceeds the ATOMIC bundle capacity."""
    if len(recipients) > MAX_PARTICIPANTS:
        raise ValueError(
            f"{len(recipients)} partecipanti superano la capacita atomica "
            f"di {MAX_PARTICIPANTS} (bundle singolo); il lancio NON sara "
            f"eseguito perche piu bundle non sono atomici tra loro")
    return [recipients[i:i + CHUNK_SIZE]
            for i in range(0, len(recipients), CHUNK_SIZE)]


async def upload_pinata_metadata(http: aiohttp.ClientSession, cfg) -> str:
    """Upload image + metadata JSON to Pinata; returns the metadata URI."""
    headers = {"Authorization": f"Bearer {cfg.pinata_jwt}"}

    with open(cfg.token_image_path, "rb") as f:
        form = aiohttp.FormData()
        form.add_field("network", "public")
        form.add_field("file", f, filename="image.png",
                       content_type="image/png")
        async with http.post("https://uploads.pinata.cloud/v3/files",
                            headers=headers, data=form) as r:
            r.raise_for_status()
            img_cid = (await r.json())["data"]["cid"]

    meta = {
        "name": cfg.token_name, "symbol": cfg.token_symbol,
        "description": cfg.token_description,
        "image": f"https://ipfs.io/ipfs/{img_cid}",
        "twitter": cfg.token_twitter, "telegram": cfg.token_telegram,
        "website": cfg.token_website,
    }
    form = aiohttp.FormData()
    form.add_field("network", "public")
    form.add_field("file", aiohttp.JsonPayload(meta),
                   filename="metadata.json",
                   content_type="application/json")
    async with http.post("https://uploads.pinata.cloud/v3/files",
                         headers=headers, data=form) as r:
        r.raise_for_status()
        meta_cid = (await r.json())["data"]["cid"]
    return f"https://ipfs.io/ipfs/{meta_cid}"


async def fetch_global_account(chain) -> bytes:
    resp = await chain.client.get_account_info(
        __import__("solders").pubkey.Pubkey.from_string(GLOBAL_ACCOUNT))
    if resp.value is None:
        raise RuntimeError("pump.fun global account non trovato")
    import base64
    return base64.b64decode(resp.value.data[0])


class PumpPortal:
    def __init__(self, http: aiohttp.ClientSession):
        self.http = http

    async def create_with_devbuy(self, payer: str, mint: str, uri: str,
                                 name: str, symbol: str,
                                 dev_buy_sol: float, slippage: float,
                                 priority_fee: float) -> str:
        """Create tx INCLUDING the dev buy. Returns base58 unsigned tx."""
        payload = {
            "publicKey": payer,
            "action": "create",
            "tokenMetadata": {"name": name, "symbol": symbol, "uri": uri},
            "mint": mint,
            "denominatedInSol": "true",
            "amount": dev_buy_sol,
            "slippage": slippage,
            "priorityFee": priority_fee,
            "pool": "pump",
        }
        async with self.http.post(f"{PUMPPORTAL_BASE}/trade-local",
                                  data=payload) as r:
            r.raise_for_status()
            return await r.text()


async def wait_bundle(http: aiohttp.ClientSession, bundle_id: str,
                      timeout_s: float = 90.0,
                      poll_s: float = 3.0) -> str:
    """Poll getBundleStatuses until Landed/Failed or timeout.
    Returns 'landed' | 'failed' | 'pending' (timeout)."""
    import asyncio
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getBundleStatuses",
                   "params": [[bundle_id]]}
        async with http.post(
                "https://mainnet.block-engine.jito.wtf/api/v1/bundles",
                json=payload) as r:
            data = await r.json()
        try:
            value = data["result"]["value"][0]
            status = value["bundle_status"]
            if status == "Landed":
                return "landed"
            if status in ("Failed", "Invalid"):
                return "failed"
        except (KeyError, IndexError):
            pass
        await asyncio.sleep(poll_s)
    return "pending"


async def run_launch(chain, cfg, conn, http: aiohttp.ClientSession) -> dict:
    """Atomic launch. Raises on any precondition failure; the ledger state
    is only committed to 'launched' after the bundle is confirmed landed."""
    global_raw = await fetch_global_account(chain)
    onchain = verify_params(global_raw)
    if not params_match(onchain):
        raise RuntimeError(
            f"parametri curva pump.fun cambiati: {onchain} — verificare "
            f"pump_curve.py prima di lanciare")

    raised = db.total_raised(conn)
    if raised <= 0:
        raise RuntimeError("nessun contributo raccolto")
    dev_buy_sol = raised * (100.0 - cfg.team_keep_percent) / 100.0
    treasury_sol = await chain.balance_sol()
    if dev_buy_sol > treasury_sol - 0.01:
        raise RuntimeError(
            f"treasury ({treasury_sol:.4f} SOL) insufficiente per il dev "
            f"buy ({dev_buy_sol:.4f} SOL): il saldo preesistente non "
            f"copre la differenza")

    from solders.keypair import Keypair
    mint_kp = Keypair()
    mint = str(mint_kp.pubkey())

    uri = await upload_pinata_metadata(http, cfg)
    portal = PumpPortal(http)
    create_b58 = await portal.create_with_devbuy(
        chain.pubkey, mint, uri, cfg.token_name, cfg.token_symbol,
        dev_buy_sol, cfg.launch_slippage, cfg.launch_priority_fee)
    tx_create = chain.sign_b58(create_b58, extra_signers=[mint_kp])

    expected = tokens_out_for_sol(dev_buy_sol)
    contributions = {u: c for u, c in service.shares(conn).items() if c > 0}
    dist, team, dust = compute_distribution(
        contributions, expected, cfg.team_share_bps, cfg.safety_bps)

    wallets = {uid: db.get_wallet(conn, uid) for uid, _ in dist}
    recipients = [(wallets[uid], base) for uid, base in dist
                 if wallets.get(uid) and base > 0]
    chunks = plan_chunks(recipients)

    tip_accounts = await chain.fetch_tip_accounts(http)
    await chain.refresh_blockhash()
    dist_txs = []
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        tip = cfg.jito_tip_lamports if is_last else 0
        signed = chain.build_distribution_tx(
            mint, chunk, tip_lamports=tip, tip_account=tip_accounts[0])
        if len(signed) > TX_SIZE_LIMIT:
            raise RuntimeError(
                f"tx distribuzione {i}: {len(signed)} byte > {TX_SIZE_LIMIT}")
        dist_txs.append(signed)

    bundle = [tx_create] + dist_txs
    bundle_id = await chain.send_bundle(http, bundle)
    status = await wait_bundle(http, bundle_id)
    if status != "landed":
        if status == "failed":
            raise RuntimeError(f"bundle fallito ({bundle_id}): "
                               f"nulla e' stato eseguito, riprovare")
        raise RuntimeError(
            f"bundle in attesa oltre il timeout ({bundle_id}): verificare "
            f"manualmente su Solscan prima di riprovare")

    sent_base = sum(b for _, b in recipients)
    db.record_launch(conn, mint, bundle_id, bundle_id, dev_buy_sol,
                     expected / 10 ** DECIMALS,
                     sent_base / 10 ** DECIMALS,
                     team / 10 ** DECIMALS)
    db.set_meta(conn, "state", "launched")
    db.set_meta(conn, "mint", mint)
    log.info("launch landed: mint=%s dev_buy=%.4f distributed=%d base units "
             "to %d wallets (team=%d, dust=%d)",
             mint, dev_buy_sol, sent_base, len(recipients), team, dust)
    return {"mint": mint, "bundle_id": bundle_id, "status": status,
            "dev_buy_sol": dev_buy_sol,
            "tokens_bought": expected / 10 ** DECIMALS,
            "distributed": sent_base / 10 ** DECIMALS,
            "team_tokens": team / 10 ** DECIMALS,
            "dust_tokens": dust / 10 ** DECIMALS,
            "wallets": len(recipients)}
