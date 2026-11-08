import sqlite3
from contextlib import contextmanager


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    wallet TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    kind TEXT NOT NULL,             -- 'buy' | 'sell'
    amount_sol REAL NOT NULL,
    amount_tokens REAL NOT NULL,
    fee_sol REAL NOT NULL DEFAULT 0,
    signature TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS launches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    sig_create TEXT,
    sig_buy TEXT,
    dev_buy_sol REAL NOT NULL,
    tokens_bought REAL NOT NULL,
    tokens_distributed REAL NOT NULL,
    team_tokens REAL NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS processed_sigs (
    signature TEXT PRIMARY KEY,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def open_conn(db_path: str) -> sqlite3.Connection:
    """Persistent connection for long-running tasks (handlers)."""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


@contextmanager
def connect(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_state(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT COALESCE(SUM(CASE kind WHEN 'buy' THEN amount_tokens "
        "ELSE -amount_tokens END), 0) AS supply FROM ledger"
    ).fetchone()
    return {"supply_tokens": row["supply"]}


def user_position(conn: sqlite3.Connection, telegram_id: int) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(CASE kind WHEN 'buy' THEN amount_tokens ELSE -amount_tokens END), 0) AS pos "
        "FROM ledger WHERE telegram_id = ?",
        (telegram_id,),
    ).fetchone()
    return row["pos"]


def record(conn: sqlite3.Connection, telegram_id: int, kind: str,
           amount_sol: float, amount_tokens: float, fee_sol: float = 0.0,
           signature: str | None = None) -> None:
    conn.execute(
        "INSERT INTO ledger (telegram_id, kind, amount_sol, amount_tokens, fee_sol, signature) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (telegram_id, kind, amount_sol, amount_tokens, fee_sol, signature),
    )


def set_wallet(conn: sqlite3.Connection, telegram_id: int, wallet: str) -> None:
    conn.execute(
        "INSERT INTO users (telegram_id, wallet) VALUES (?, ?) "
        "ON CONFLICT(telegram_id) DO UPDATE SET wallet = excluded.wallet",
        (telegram_id, wallet),
    )


def get_wallet(conn: sqlite3.Connection, telegram_id: int) -> str | None:
    row = conn.execute("SELECT wallet FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
    return row["wallet"] if row else None


def sig_seen(conn: sqlite3.Connection, signature: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM processed_sigs WHERE signature = ?", (signature,)
    ).fetchone() is not None


def mark_sig(conn: sqlite3.Connection, signature: str) -> None:
    conn.execute("INSERT OR IGNORE INTO processed_sigs (signature) VALUES (?)", (signature,))


def record_launch(conn: sqlite3.Connection, mint: str, sig_create: str,
                  sig_buy: str, dev_buy_sol: float, tokens_bought: float,
                  tokens_distributed: float, team_tokens: float) -> None:
    conn.execute(
        "INSERT INTO launches (mint, sig_create, sig_buy, dev_buy_sol, "
        "tokens_bought, tokens_distributed, team_tokens) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (mint, sig_create, sig_buy, dev_buy_sol, tokens_bought,
         tokens_distributed, team_tokens),
    )


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None
