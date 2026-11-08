"""Background watcher: SOL deposits to the treasury become token credits."""

from __future__ import annotations

import asyncio
import logging

from . import db, service
from .curve import LinearCurve

log = logging.getLogger("monitor")


class DepositMonitor:
    def __init__(self, chain, cfg, conn_factory):
        self.chain = chain
        self.cfg = cfg
        self.conn_factory = conn_factory
        self.last_signature: str | None = None

    async def run(self):
        while True:
            try:
                await self._poll_sol_deposits()
            except Exception:
                log.exception("deposit poll failed")
            await asyncio.sleep(self.cfg.poll_interval_s)

    async def _poll_sol_deposits(self):
        with self.conn_factory() as conn:
            wallet_map = {
                row["wallet"]: row["telegram_id"]
                for row in conn.execute(
                    "SELECT telegram_id, wallet FROM users WHERE wallet IS NOT NULL"
                )
            }
            transfers = await self.chain.incoming_sol_transfers()
            for t in transfers:
                if t["signature"] == self.last_signature:
                    break
                if db.sig_seen(conn, t["signature"]):
                    continue
                await self._process_transfer(conn, t, wallet_map)
                db.mark_sig(conn, t["signature"])
            if transfers:
                self.last_signature = transfers[0]["signature"]

    async def _process_transfer(self, conn, transfer, wallet_map):
        curve = LinearCurve(self.cfg.price_base_sol, self.cfg.slope_sol)
        sender = transfer["sender"]
        amount_sol = transfer["amount_sol"]
        telegram_id = wallet_map.get(sender)
        if telegram_id is None:
            log.warning("deposit from unknown wallet %s (%.6f SOL), ignored",
                        sender, amount_sol)
            return
        if amount_sol < self.cfg.min_deposit_sol:
            log.info("deposit below minimum from %s, ignored", sender)
            return
        result = service.buy(conn, curve, telegram_id, amount_sol,
                             self.cfg.buy_fee_bps,
                             signature=transfer["signature"])
        log.info("buy: user=%s sol=%.6f fee=%.6f tokens=%.4f",
                 telegram_id, amount_sol, result["fee_sol"], result["tokens"])
