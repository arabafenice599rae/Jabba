"""Launch the presale on pump.fun: create token, dev buy, instant distribution.

Flow (all client-signed — keys never leave the bot):
  1. Create the token via PumpPortal local API (free, dev buy bundled)
  2. Dev-buy on the curve with the presale SOL (bottom price)
  3. Distribute the bought tokens to participants' wallets immediately,
     proportionally to their presale positions (team share configurable)

pump.fun is mainnet-only: this module cannot be tested on devnet.
"""

from __future__ import annotations

import logging

import aiohttp

from . import db

log = logging.getLogger("launch")

PUMPPORTAL_BASE = "https://pumpportal.fun/api"


def compute_distribution(positions: dict[int, float], tokens: float,
                         team_bps: int) -> tuple[list[tuple[int, float]], float]:
    """Split `tokens` proportionally to presale positions.

    positions: telegram_id -> token position (presale credits)
    team_bps:  share retained by the treasury, in basis points
    Returns ([(telegram_id, tokens)], team_tokens). Pure function, testable.
    """
    if not positions or tokens <= 0:
        return [], 0.0
    total = sum(positions.values())
    if total <= 0:
        return [], 0.0
    team = tokens * team_bps / 10_000.0
    distributable = tokens - team
    out = []
    assigned = 0.0
    ids = sorted(positions)
    for i, uid in enumerate(ids):
        if i == len(ids) - 1:
            share = distributable - assigned
        else:
            share = distributable * positions[uid] / total
        out.append((uid, share))
        assigned += share
    return out, team


class PumpPortal:
    """Thin client for the PumpPortal local (client-signed) API."""

    def __init__(self, http: aiohttp.ClientSession):
        self.http = http

    async def create_token(self, mint_pubkey: str, name: str, symbol: str,
                           description: str, image_path: str,
                           twitter: str = "", telegram: str = "",
                           website: str = "") -> str:
        """Returns base58-encoded unsigned transaction to sign and send."""
        form = aiohttp.FormData()
        form.add_field("mint", mint_pubkey)
        form.add_field("name", name)
        form.add_field("symbol", symbol)
        form.add_field("description", description)
        form.add_field("twitter", twitter)
        form.add_field("telegram", telegram)
        form.add_field("website", website)
        form.add_field("showName", "true")
        form.add_field("pinned", "true")
        with open(image_path, "rb") as f:
            form.add_field("file", f, filename="image.png",
                           content_type="image/png")
        async with self.http.post(f"{PUMPPORTAL_BASE}/create", data=form) as r:
            r.raise_for_status()
            return await r.text()

    async def buy(self, payer_pubkey: str, mint: str, amount_sol: float,
                  slippage: float = 10.0, priority_fee: float = 0.0005) -> str:
        """Buy on the bonding curve. Returns base58 unsigned transaction."""
        payload = {
            "publicKey": payer_pubkey,
            "action": "buy",
            "mint": mint,
            "amount": amount_sol,
            "denominatedInSol": "true",
            "slippage": slippage,
            "priorityFee": priority_fee,
            "pool": "pump",
        }
        async with self.http.post(f"{PUMPPORTAL_BASE}/trade-local",
                                   data=payload) as r:
            r.raise_for_status()
            return await r.text()


async def run_launch(chain, cfg, conn, http: aiohttp.ClientSession) -> dict:
    """ATOMIC launch via a Jito bundle: create + dev buy + distribution,
    all in the same slot. If any tx fails, everything reverts — the treasury
    never holds tokens in any confirmed on-chain state.

    The distribution can be pre-built because the token is created inside
    the same bundle: its curve is virgin, so the dev-buy output is exactly
    computable offline (pump_curve.tokens_out_for_sol).

    Jito bundles accept up to 5 txs: create + buy + 3 distribution chunks
    of 15 users each -> max 45 participants per atomic launch.
    """
    from solders.keypair import Keypair

    from .pump_curve import tokens_out_for_sol

    portal = PumpPortal(http)
    mint_kp = Keypair()
    mint = str(mint_kp.pubkey())

    create_b58 = await portal.create_token(
        mint, cfg.token_name, cfg.token_symbol,
        cfg.token_description, cfg.token_image_path,
        cfg.token_twitter, cfg.token_telegram, cfg.token_website,
    )
    tx_create = chain.sign_b58(create_b58, extra_signers=[mint_kp])

    treasury_sol = await chain.balance_sol()
    dev_buy_sol = treasury_sol * cfg.dev_buy_percent / 100.0
    dev_buy_sol = min(dev_buy_sol, treasury_sol - 0.01)
    if dev_buy_sol <= 0:
        raise RuntimeError("treasury SOL insufficient for dev buy")

    buy_b58 = await portal.buy(chain.pubkey, mint, dev_buy_sol,
                              slippage=cfg.launch_slippage,
                              priority_fee=cfg.launch_priority_fee)
    tx_buy = chain.sign_b58(buy_b58)

    expected = tokens_out_for_sol(dev_buy_sol)
    contributions = {
        uid: c for uid, c in service.shares(conn).items() if c > 0
    }
    dist, team = compute_distribution(contributions, expected,
                                      cfg.team_share_bps)

    wallets = {uid: db.get_wallet(conn, uid) for uid, _ in dist}
    recipients = [
        (wallets[uid], amount) for uid, amount in dist
        if wallets.get(uid) and amount > 0
    ]
    CHUNK = 15
    chunks = [recipients[i:i + CHUNK] for i in range(0, len(recipients), CHUNK)]
    if 2 + len(chunks) > 5:
        raise RuntimeError(
            f"troppi partecipanti ({len(recipients)}) per un bundle atomico "
            f"(max 45); ridurre i partecipanti o lanciare in modalita non atomica")

    tip_accounts = await chain.fetch_tip_accounts(http)
    tip_account = tip_accounts[0]
    await chain.refresh_blockhash()

    dist_txs = []
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        tip = cfg.jito_tip_lamports if is_last else 0
        dist_txs.append(chain.build_distribution_tx(
            mint, chunk, tip_lamports=tip, tip_account=tip_account))

    bundle = [tx_create, tx_buy] + dist_txs
    bundle_id = await chain.send_bundle(http, bundle)
    log.info("atomic bundle %s submitted: mint=%s dev_buy=%.4f SOL "
             "expected=%.0f tokens to %d wallets",
             bundle_id, mint, dev_buy_sol, expected, len(recipients))

    sent = sum(a for _, a in recipients)
    db.record_launch(conn, mint, bundle_id, bundle_id, dev_buy_sol, expected,
                     sent, team)
    db.set_meta(conn, "state", "launched")
    db.set_meta(conn, "mint", mint)
    return {"mint": mint, "bundle_id": bundle_id,
            "dev_buy_sol": dev_buy_sol, "tokens_bought": expected,
            "distributed": sent, "team_tokens": team,
            "wallets": len(recipients)}
