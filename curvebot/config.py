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

    refund_fee_bps: int = field(default_factory=lambda: int(_env("REFUND_FEE_BPS", "30")))

    token_name: str = field(default_factory=lambda: _env("TOKEN_NAME", ""))
    token_symbol: str = field(default_factory=lambda: _env("TOKEN_SYMBOL", ""))
    token_description: str = field(default_factory=lambda: _env("TOKEN_DESCRIPTION", ""))
    token_image_path: str = field(default_factory=lambda: _env("TOKEN_IMAGE_PATH", "token.png"))
    pinata_jwt: str = field(default_factory=lambda: _env("PINATA_JWT", ""))
    safety_bps: int = field(default_factory=lambda: int(_env("SAFETY_BPS", "10")))
    token_twitter: str = field(default_factory=lambda: _env("TOKEN_TWITTER", ""))
    token_telegram: str = field(default_factory=lambda: _env("TOKEN_TELEGRAM", ""))
    token_website: str = field(default_factory=lambda: _env("TOKEN_WEBSITE", ""))
    team_keep_percent: float = field(default_factory=lambda: float(_env("TEAM_KEEP_PERCENT", "10")))
    team_share_bps: int = field(default_factory=lambda: int(_env("TEAM_SHARE_BPS", "0")))
    launch_slippage: float = field(default_factory=lambda: float(_env("LAUNCH_SLIPPAGE", "10")))
    launch_priority_fee: float = field(default_factory=lambda: float(_env("LAUNCH_PRIORITY_FEE", "0.0005")))
    jito_tip_lamports: int = field(default_factory=lambda: int(_env("JITO_TIP_LAMPORTS", "1000000")))

    allowed_ids: list[int] = field(default_factory=lambda: [
        int(x) for x in _env("ALLOWED_TELEGRAM_IDS").split(",") if x.strip()
    ])
    min_deposit_sol: float = field(default_factory=lambda: float(_env("MIN_DEPOSIT_SOL", "0.01")))
    poll_interval_s: float = field(default_factory=lambda: float(_env("POLL_INTERVAL_S", "5")))
