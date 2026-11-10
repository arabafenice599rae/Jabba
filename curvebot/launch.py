"""Atomic launch on pump.fun via a Jito bundle: create+devbuy (ONE tx) +
distribution chunks, all in the same slot. If any tx fails, everything
reverts — the treasury never holds tokens in any confirmed state.

Verified facts (2026-11):
- The first tx is OUR create_v2 + treasury ATA + buy_v3 of an EXACT token
  amount, built on the pump.fun program (pump_ix.py, official IDL): no
  PumpPortal, no third-party router. Metadata is uploaded to IPFS (Pinata)
  beforehand.
- The dev buy is DYNAMIC (plan_dev_buy): the target share of the raise,
  lowered to what the treasury can pay after every exact cost.
- pump.fun global account: VTR 1.073B, VSR 30 SOL, fee 95 bps protocol
  (+30 bps creator), 6 decimals, supply 1B. Verified at launch time.
- The mint is Token-2022; fees are paid on top of the curve SOL (see
  pump_curve). All of this is re-checked by tests/test_fork_mainnet.py.

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

import asyncio
import json
import logging
import time
from fractions import Fraction
from pathlib import Path

import aiohttp

from . import db, service
from .chain import TOKEN_2022_PROGRAM
from .pump_curve import (
    DECIMALS,
    MAX_DEV_BUY_LAMPORTS,
    TOKEN_ACCOUNT_SIZE,
    classic_rent,
    create_account_sizes,
    curve_sol_affordable,
    max_sol_cost_lamports,
    params_match,
    sol_to_lamports,
    tokens_out_for_sol,
    verify_params,
)

log = logging.getLogger("launch")

GLOBAL_ACCOUNT = "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"
TX_SIZE_LIMIT = 1232
BYTES_PER_RECIPIENT = 92          # measured: ATA create + transfer_checked
TIP_OVERHEAD = 64                 # measured: tip transfer ix + account key
BASE_TX_OVERHEAD = 294            # measured: header + shared accounts
CHUNK_SIZE = 9                    # (1232 - 294 - 64) // 92 == 9
METADATA_URI_BUDGET = "https://ipfs.io/ipfs/" + "b" * 59   # a Pinata CIDv1 URI
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
    if team_bps < 0 or safety_bps < 0 or team_bps + safety_bps > 10_000:
        raise ValueError("team_bps + safety_bps deve stare in [0, 10000]")
    if not weights or tokens_base <= 0:
        return [], 0, 0
    exact = {uid: Fraction(w) for uid, w in weights.items()}   # floats are exact here
    if any(w < 0 for w in exact.values()):
        raise ValueError("peso negativo nella distribuzione")
    total_w = sum(exact.values())
    if total_w <= 0:
        return [], 0, 0
    team = tokens_base * team_bps // 10_000
    distributable = tokens_base * (10_000 - team_bps - safety_bps) // 10_000
    out = []
    assigned = 0
    for uid in sorted(exact):
        share = int(distributable * exact[uid] // total_w)
        out.append((uid, share))
        assigned += share
    dust = distributable - assigned
    return out, team, dust


NETWORK_FEE_LAMPORTS = 5_000              # per signature


async def _rent(chain, size: int) -> int:
    try:
        return (await chain.client.get_minimum_balance_for_rent_exemption(size)).value
    except Exception:  # noqa: BLE001 - RPC hiccup: the classic rent is an upper bound
        return classic_rent(size)


async def estimate_launch_costs(chain, cfg, n_recipients: int,
                                dev_buy_sol: float) -> dict:
    """Full cost of the launch, in lamports, from LIVE rent:
    dev buy at the fee cap + rent of the accounts the create opens + ATA rent
    for every recipient (worst case: none exists yet) + signatures + tip +
    the treasury's own rent-exempt minimum.
    No priority fee: inside a Jito bundle the tip pays for inclusion."""
    sizes = create_account_sizes(cfg.token_name or "x", cfg.token_symbol or "x",
                                 METADATA_URI_BUDGET)
    create_rent = 0
    for size in sizes:
        create_rent += await _rent(chain, size)
    n_dist_txs = max(1, -(-n_recipients // CHUNK_SIZE))
    breakdown = {
        "dev_buy": max_sol_cost_lamports(dev_buy_sol),
        "create_rent": create_rent,
        "ata_rent": await _rent(chain, TOKEN_ACCOUNT_SIZE) * n_recipients,
        "network_fees": NETWORK_FEE_LAMPORTS * (2 + n_dist_txs),
        "jito_tip": cfg.jito_tip_lamports,
        # the treasury must stay rent-exempt after the bundle, otherwise
        # the last tx fails with InsufficientFundsForRent (found on the fork)
        "treasury_reserve": await _rent(chain, 0),
    }
    breakdown["total"] = sum(breakdown.values())
    return breakdown


async def plan_dev_buy(chain, cfg, n_recipients: int, raised_sol: float,
                       treasury_lamports: int) -> tuple[float, dict]:
    """DYNAMIC dev buy: the target is raised x (100 - TEAM_KEEP_PERCENT)%,
    lowered if needed to what the treasury can pay after every exact cost.
    The team gets whatever is left. Refuses when the dev buy would fall
    below MIN_DEV_BUY_PERCENT of the raise (costs too large for the raise)
    or reach the curve's real reserves."""
    target = raised_sol * (100.0 - cfg.team_keep_percent) / 100.0
    fixed = await estimate_launch_costs(chain, cfg, n_recipients, 0.0)
    budget = treasury_lamports - (fixed["total"] - fixed["dev_buy"])
    affordable = curve_sol_affordable(budget) / 1_000_000_000
    dev = min(target, affordable)
    floor = raised_sol * cfg.min_dev_buy_percent / 100.0
    if dev <= 0 or dev < floor:
        raise RuntimeError(
            f"raccolta troppo piccola per i costi fissi: il dev buy sarebbe "
            f"{max(dev, 0):.6f} SOL, sotto il minimo del "
            f"{cfg.min_dev_buy_percent:.0f}% della raccolta ({floor:.6f} SOL). "
            f"Costi fissi {(fixed['total'] - fixed['dev_buy']) / 1e9:.6f} SOL, "
            f"treasury {treasury_lamports / 1e9:.6f} SOL")
    if sol_to_lamports(dev) >= MAX_DEV_BUY_LAMPORTS:
        raise RuntimeError(
            f"dev buy {dev:.4f} SOL >= capacita della curva "
            f"({MAX_DEV_BUY_LAMPORTS / 1e9:.3f} SOL): la distribuzione "
            f"supererebbe le riserve reali")
    costs = await estimate_launch_costs(chain, cfg, n_recipients, dev)
    if costs["total"] > treasury_lamports:          # defensive: never sign
        raise RuntimeError("treasury insufficiente per il dev buy pianificato")
    costs["team_left"] = treasury_lamports - costs["total"]
    return dev, costs


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

    image = await asyncio.to_thread(Path(cfg.token_image_path).read_bytes)
    form = aiohttp.FormData()
    form.add_field("network", "public")
    form.add_field("file", image, filename="image.png",
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
    """Raw data of the pump.fun global account. solana-py already decodes
    the base64 payload: `value.data` is bytes."""
    from solders.pubkey import Pubkey

    resp = await chain.client.get_account_info(Pubkey.from_string(GLOBAL_ACCOUNT))
    if resp.value is None:
        raise RuntimeError("pump.fun global account non trovato")
    return bytes(resp.value.data)


def parse_inflight_status(data: dict) -> str:
    """getInflightBundleStatuses: status is a single-key map
    {Pending|Processing|Failed|Landed: {...}}."""
    try:
        status = data["result"]["value"][0]["status"]
        if isinstance(status, dict):
            return next(iter(status.keys()))
        return str(status)
    except (KeyError, IndexError, TypeError):
        return "Unknown"


def parse_confirmation(data: dict) -> str:
    """getBundleStatuses: per-tx confirmation_status
    (processed|confirmed|finalized) once the bundle has Landed."""
    try:
        txs = data["result"]["value"][0]["transactions"]
        statuses = {t.get("confirmation_status") for t in txs}
        if "finalized" in statuses:
            return "finalized"
        if "confirmed" in statuses:
            return "confirmed"
        if "processed" in statuses:
            return "processed"
        return "unknown"
    except (KeyError, IndexError, TypeError):
        return "unknown"


async def wait_bundle(http: aiohttp.ClientSession, bundle_id: str,
                      land_timeout_s: float = 90.0,
                      confirm_timeout_s: float = 30.0,
                      poll_s: float = 3.0) -> str:
    """Two-phase bundle verification.

    Phase 1 — getInflightBundleStatuses until Landed/Failed.
    Phase 2 — after Landed, getBundleStatuses until confirmed/finalized.

    Returns: 'finalized' | 'confirmed' | 'failed' | 'pending' (timeout:
    UNKNOWN outcome -> manual reconciliation, NEVER auto-retry)."""
    import time

    async def rpc(method, params):
        payload = {"jsonrpc": "2.0", "id": 1, "method": method,
                   "params": params}
        async with http.post(
                "https://mainnet.block-engine.jito.wtf/api/v1/bundles",
                json=payload) as r:
            return await r.json()

    # phase 1: landing
    deadline = time.monotonic() + land_timeout_s
    while time.monotonic() < deadline:
        status = parse_inflight_status(
            await rpc("getInflightBundleStatuses", [[bundle_id]]))
        if status == "Landed":
            break
        if status == "Failed":
            return "failed"
        await asyncio.sleep(poll_s)
    else:
        return "pending"

    # phase 2: confirmation
    deadline = time.monotonic() + confirm_timeout_s
    while time.monotonic() < deadline:
        conf = parse_confirmation(await rpc("getBundleStatuses", [[bundle_id]]))
        if conf in ("confirmed", "finalized"):
            return conf
        await asyncio.sleep(poll_s)
    return "confirmed"  # landed but confirmation not yet observed


async def run_launch(chain, cfg, conn, http: aiohttp.ClientSession) -> dict:
    """Atomic launch behind a PERSISTENT lock (meta table, atomic
    check-and-set): two launches can never run, even across restarts.
    Released on a DEFINITIVE failure (nothing executed); kept when the
    bundle outcome is UNKNOWN, so refunds stay suspended until the admin
    has checked on chain. State becomes 'launched' only after landing."""
    if not db.acquire_launch_lock(conn):
        raise RuntimeError("lancio già in corso o esito precedente da "
                           "verificare (lock attivo)")
    try:
        return await _run_launch_locked(chain, cfg, conn, http)
    except LaunchOutcomeUnknown:
        raise                           # lock stays: manual verification
    except BaseException:
        db.release_launch_lock(conn)
        raise


class LaunchOutcomeUnknown(RuntimeError):
    """The bundle may or may not have landed: never retry blindly."""


BLOCKHASH_EXPIRY_S = 300   # a bundle's blockhash is long dead after 5 minutes


async def resolve_unknown_launch(chain, conn, now: float | None = None) -> str:
    """Settle a 'launch_unknown' state FROM THE CHAIN, never by guess.
    The bundle is atomic: the mint exists (finalized) iff it landed.
    -> "landed": state 'launched' (lock kept, refunds stay closed)
    -> "failed": mint absent after the blockhash expired; lock released
    -> "unknown": too early to tell, nothing changes"""
    from solana.rpc.core import Finalized
    from solders.pubkey import Pubkey

    if db.get_meta(conn, "state") != "launch_unknown":
        raise RuntimeError("nessun lancio con esito sconosciuto")
    mint = db.get_meta(conn, "pending_mint") or ""
    resp = await chain.client.get_account_info(Pubkey.from_string(mint),
                                               commitment=Finalized)
    if resp.value is not None:
        # same bookkeeping as the normal path: /sweep and /launch read `mint`
        bundle_id = db.get_meta(conn, "pending_bundle") or ""
        rep = json.loads(db.get_meta(conn, "pending_report") or "{}")
        if rep:
            db.record_launch(conn, mint, bundle_id, bundle_id, rep["dev_buy_sol"],
                             rep["tokens_bought"], rep["distributed"],
                             rep["team_tokens"])
        db.set_meta(conn, "mint", mint)
        db.set_meta(conn, "state", "launched")
        return "landed"
    age = (now or time.time()) - int(db.get_meta(conn, "pending_at") or 0)
    if age < BLOCKHASH_EXPIRY_S:
        return "unknown"
    db.set_meta(conn, "state", "")
    db.release_launch_lock(conn)
    return "failed"


async def _run_launch_locked(chain, cfg, conn, http: aiohttp.ClientSession) -> dict:
    """Launch body; the caller holds the launch lock."""
    global_raw = await fetch_global_account(chain)
    onchain = verify_params(global_raw)
    if not params_match(onchain):
        raise RuntimeError(
            f"parametri curva pump.fun cambiati: {onchain} — verificare "
            f"pump_curve.py prima di lanciare")

    raised = db.total_raised(conn)
    if raised <= 0:
        raise RuntimeError("nessun contributo raccolto")
    contributions = {u: c for u, c in service.shares(conn).items() if c > 0}
    treasury_lamports = int((await chain.balance_sol()) * 1_000_000_000)
    dev_buy_sol, costs = await plan_dev_buy(
        chain, cfg, len(contributions), raised, treasury_lamports)

    from solders.keypair import Keypair
    mint_kp = Keypair()
    mint = str(mint_kp.pubkey())

    uri = await upload_pinata_metadata(http, cfg)
    amount = tokens_out_for_sol(dev_buy_sol)       # buy_v3 buys EXACTLY this
    await chain.refresh_blockhash()
    tx_create = chain.build_create_tx(
        mint_kp, cfg.token_name, cfg.token_symbol, uri, amount,
        max_sol_cost_lamports(dev_buy_sol))
    if len(tx_create) > TX_SIZE_LIMIT:
        raise RuntimeError(f"tx di creazione {len(tx_create)} byte > "
                           f"{TX_SIZE_LIMIT}: accorciare nome, simbolo o uri")
    token_program = TOKEN_2022_PROGRAM

    expected = amount
    dist, team, dust = compute_distribution(
        contributions, expected, cfg.team_share_bps, cfg.safety_bps)

    wallets = {uid: db.get_wallet(conn, uid) for uid, _ in dist}
    recipients = [(wallets[uid], base) for uid, base in dist
                 if wallets.get(uid) and base > 0]
    chunks = plan_chunks(recipients)

    tip_accounts = await chain.fetch_tip_accounts(http)
    dist_txs = []
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        tip = cfg.jito_tip_lamports if is_last else 0
        signed = chain.build_distribution_tx(
            mint, chunk, tip_lamports=tip, tip_account=tip_accounts[0],
            token_program=token_program)
        if len(signed) > TX_SIZE_LIMIT:
            raise RuntimeError(
                f"tx distribuzione {i}: {len(signed)} byte > {TX_SIZE_LIMIT}")
        dist_txs.append(signed)

    bundle = [tx_create, *dist_txs]
    bundle_id = await chain.send_bundle(http, bundle)
    status = await wait_bundle(http, bundle_id)
    if status == "failed":
        raise RuntimeError(f"bundle fallito ({bundle_id}): "
                           f"nulla e' stato eseguito, riprovare")
    if status == "pending":
        db.set_meta(conn, "state", "launch_unknown")
        db.set_meta(conn, "pending_mint", mint)
        db.set_meta(conn, "pending_bundle", bundle_id)
        db.set_meta(conn, "pending_at", str(int(time.time())))
        db.set_meta(conn, "pending_report", json.dumps({
            "dev_buy_sol": dev_buy_sol,
            "tokens_bought": expected / 10 ** DECIMALS,
            "distributed": sum(b for _, b in recipients) / 10 ** DECIMALS,
            "team_tokens": team / 10 ** DECIMALS}))
        raise LaunchOutcomeUnknown(
            f"esito bundle SCONOSCIUTO ({bundle_id}): verificare "
            f"manualmente su Solscan PRIMA di riprovare (mai retry "
            f"automatico: rischio doppio lancio)")

    sent_base = sum(b for _, b in recipients)
    db.record_launch(conn, mint, bundle_id, bundle_id, dev_buy_sol,
                     expected / 10 ** DECIMALS,
                     sent_base / 10 ** DECIMALS,
                     team / 10 ** DECIMALS)
    db.set_meta(conn, "state", "launched")
    db.set_meta(conn, "mint", mint)
    log.info("launch landed: mint=%s dev_buy=%.4f distributed=%d base units "
             "to %d wallets (team=%d, dust=%d, SOL left to team ~%.6f)",
             mint, dev_buy_sol, sent_base, len(recipients), team, dust,
             costs["team_left"] / 1e9)
    return {"mint": mint, "bundle_id": bundle_id, "status": status,
            "dev_buy_sol": dev_buy_sol,
            "tokens_bought": expected / 10 ** DECIMALS,
            "distributed": sent_base / 10 ** DECIMALS,
            "team_tokens": team / 10 ** DECIMALS,
            "dust_tokens": dust / 10 ** DECIMALS,
            "team_sol_left": costs["team_left"] / 1e9,
            "wallets": len(recipients)}
