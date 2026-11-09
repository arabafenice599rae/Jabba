"""Entry point: run Telegram polling + deposit monitor."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from . import db, service
from .chain import Chain
from .config import Config
from .handlers import router
from .monitor import DepositMonitor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("main")


async def run() -> None:
    cfg = Config()
    if not cfg.telegram_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN mancante")
    if not cfg.allowed_ids:
        raise SystemExit("ALLOWED_TELEGRAM_IDS mancante: il bot rifiuterebbe chiunque")

    chain = Chain(cfg.rpc_url, cfg.keypair_path)
    log.info("treasury=%s", chain.pubkey)

    bot = Bot(cfg.telegram_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp["cfg"] = cfg
    dp["chain"] = chain
    dp.include_router(router)

    conn = db.open_conn(cfg.db_path)
    dp["conn"] = conn
    moved = service.recover_after_restart(conn)
    if moved:
        log.warning("%d rimborsi interrotti al riavvio: stato unknown, "
                    "usare /reconcile", moved)

    monitor = DepositMonitor(chain, cfg, lambda: db.connect(cfg.db_path))

    await asyncio.gather(
        dp.start_polling(bot),
        monitor.run(),
    )


if __name__ == "__main__":
    asyncio.run(run())
