"""Background watcher: SOL deposits to the treasury become token credits."""

from __future__ import annotations

import asyncio
import logging

from . import db, service

log = logging.getLogger("monitor")


class DepositMonitor:
    def __init__(self, chain, cfg, conn_factory):
        self.chain = chain
        self.cfg = cfg
        self.conn_factory = conn_factory

    async def run(self):
        while True:
            try:
                await self._poll_sol_deposits()
            except Exception:
                log.exception("deposit poll failed")
            await asyncio.sleep(self.cfg.poll_interval_s)

    async def _poll_sol_deposits(self):
        with self.conn_factory() as conn:
            state = db.get_meta(conn, "state")
            closed = state == "launched" or db.launch_lock_held(conn)
            wallet_map = {
                row["wallet"]: row["telegram_id"]
                for row in conn.execute(
                    "SELECT telegram_id, wallet FROM users WHERE wallet IS NOT NULL"
                )
            }
            transfers, examined = await self.chain.scan_incoming(
                is_seen=lambda sig: db.sig_seen(conn, sig))
            # credit + mark-as-seen in ONE transaction: a crash in between can
            # neither lose a deposit nor credit it twice on restart
            conn.execute("BEGIN IMMEDIATE")
            try:
                for t in transfers:
                    key = f"{t['signature']}:{t.get('index', 0)}"
                    if db.sig_seen(conn, key):
                        continue
                    if closed:
                        # after the launch snapshot: never credited, shows up
                        # in /reconcile as missing_in_ledger for a manual refund
                        log.warning("deposit after launch snapshot NOT credited: "
                                    "%s (%.6f SOL) %s", t["sender"],
                                    t["amount_sol"], key[:16])
                    else:
                        await self._process_transfer(conn, t, wallet_map)
                    db.mark_sig(conn, key)
                for sig in examined:
                    db.mark_sig(conn, sig)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    async def _process_transfer(self, conn, transfer, wallet_map):
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
        key = f"{transfer['signature']}:{transfer.get('index', 0)}"
        service.record_deposit(conn, telegram_id, amount_sol,
                               signature=key)
        log.info("deposit: user=%s sol=%.6f (%s)", telegram_id, amount_sol,
                 key[:16])
