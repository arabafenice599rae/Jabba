"""Presale ledger operations: SOL deposits, refunds, contribution shares.

No bonding curve of our own: the price is decided by the pump.fun curve
at launch (dev buy at the bottom). The presale only tracks who gave how
much SOL; tokens are distributed proportionally to SOL contributed.
"""

from __future__ import annotations

from . import db


def record_deposit(conn, telegram_id: int, amount_sol: float,
                   signature: str | None = None) -> None:
    db.record(conn, telegram_id, "deposit", amount_sol, 0.0,
              signature=signature)


def refund_quote(conn, telegram_id: int, fraction: float,
                 refund_fee_bps: int) -> dict:
    """Quote a partial/full refund. fraction in [0,1]. Returns dict with
    contribution_after, refund_sol, fee_sol, net_sol. Raises ValueError."""
    contribution = db.user_contribution(conn, telegram_id)
    refund_sol = contribution * fraction
    if refund_sol <= 0:
        raise ValueError("nessun contributo da rimborsare")
    fee = refund_sol * refund_fee_bps / 10_000.0
    net = refund_sol - fee
    if net <= 0:
        raise ValueError("importo troppo piccolo")
    return {
        "refund_sol": refund_sol,
        "fee_sol": fee,
        "net_sol": net,
        "contribution_after": contribution - refund_sol,
    }


def confirm_refund(conn, telegram_id: int, refund_sol: float,
                   fee_sol: float, signature: str | None = None) -> None:
    db.record(conn, telegram_id, "refund", refund_sol, fee_sol,
              signature=signature)


def shares(conn) -> dict[int, float]:
    """SOL contribution per user (net of refunds)."""
    return {
        row["telegram_id"]: db.user_contribution(conn, row["telegram_id"])
        for row in conn.execute("SELECT DISTINCT telegram_id FROM ledger")
    }


def total_raised(conn) -> float:
    return db.total_raised(conn)
