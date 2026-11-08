import os
from dataclasses import dataclass, field


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


@dataclass
class Config:
    telegram_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    rpc_url: str = field(default_factory=lambda: _env("RPC_URL", "https://api.devnet.solana.com"))
    keypair_path: str = field(default_factory=lambda: _env("KEYPAIR_PATH", "treasury.json"))
    db_path: str = field(default_factory=lambda: _env("DB_PATH", "curvebot.db"))

    price_base_sol: float = field(default_factory=lambda: float(_env("PRICE_BASE_SOL", "0.00001")))
    slope_sol: float = field(default_factory=lambda: float(_env("SLOPE_SOL", "0.0000000005")))
    buy_fee_bps: int = field(default_factory=lambda: int(_env("BUY_FEE_BPS", "30")))
    sell_fee_bps: int = field(default_factory=lambda: int(_env("SELL_FEE_BPS", "30")))

    allowed_ids: list[int] = field(default_factory=lambda: [
        int(x) for x in _env("ALLOWED_TELEGRAM_IDS").split(",") if x.strip()
    ])
    webapp_url: str = field(default_factory=lambda: _env("WEBAPP_URL", ""))
    min_deposit_sol: float = field(default_factory=lambda: float(_env("MIN_DEPOSIT_SOL", "0.01")))
    poll_interval_s: float = field(default_factory=lambda: float(_env("POLL_INTERVAL_S", "5")))
