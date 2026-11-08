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
    """Full launch: create -> dev buy -> distribute. Returns a report dict.

    The presale SOL in the treasury funds the dev buy. Tokens are held by
    the treasury only for the seconds between the buy and the transfers.
    """
    import base64

    import solders.transaction as txmod
    from solders.keypair import Keypair
    from solders.pubkey import Pubkey

    portal = PumpPortal(http)
    from solders.keypair import Keypair as KP
    mint_kp = KP()

    create_tx = await portal.create_token(
        str(mint_kp.pubkey()), cfg.token_name, cfg.token_symbol,
        cfg.token_description, cfg.token_image_path,
        cfg.token_twitter, cfg.token_telegram, cfg.token_website,
    )
    sig_create = await chain.sign_send_b58(create_tx, extra_signers=[mint_kp])
    mint = str(mint_kp.pubkey())
    log.info("token created: %s (%s)", mint, sig_create)

    treasury_sol = await chain.balance_sol()
    dev_buy_sol = treasury_sol * cfg.dev_buy_percent / 100.0
    dev_buy_sol = min(dev_buy_sol, treasury_sol - 0.01)
    if dev_buy_sol <= 0:
        raise RuntimeError("treasury SOL insufficient for dev buy")

    buy_tx = await portal.buy(chain.pubkey, mint, dev_buy_sol,
                              slippage=cfg.launch_slippage,
                              priority_fee=cfg.launch_priority_fee)
    sig_buy = await chain.sign_send_b58(buy_tx)
    log.info("dev buy: %.4f SOL (%s)", dev_buy_sol, sig_buy)

    bought = await chain.token_balance(mint)
    positions = {
        row["telegram_id"]: db.user_position(conn, row["telegram_id"])
        for row in conn.execute("SELECT DISTINCT telegram_id FROM ledger")
    }
    positions = {uid: p for uid, p in positions.items() if p > 0}
    dist, team = compute_distribution(positions, bought, cfg.team_share_bps)

    wallets = {uid: db.get_wallet(conn, uid) for uid, _ in dist}
    sent = 0
    for uid, amount in dist:
        wallet = wallets.get(uid)
        if not wallet or amount <= 0:
            continue
        await chain.transfer_tokens(mint, wallet, amount)
        sent += amount
    log.info("distributed %.2f tokens to %d wallets (team %.2f)",
             sent, len(dist), team)

    db.record_launch(conn, mint, sig_create, sig_buy, dev_buy_sol, bought,
                     sent, team)
    db.set_meta(conn, "state", "launched")
    db.set_meta(conn, "mint", mint)
    return {"mint": mint, "sig_create": sig_create, "sig_buy": sig_buy,
            "dev_buy_sol": dev_buy_sol, "tokens_bought": bought,
            "distributed": sent, "team_tokens": team, "wallets": len(dist)}
