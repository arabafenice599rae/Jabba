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


def available_contribution(conn, telegram_id: int) -> float:
    """Contribution minus outstanding refund reservations."""
    return db.user_contribution(conn, telegram_id) - \
        db.pending_refund_total(conn, telegram_id)


def reserve_refund(conn, telegram_id: int, fraction: float,
                   refund_fee_bps: int) -> dict:
    """Atomically reserve a refund (concurrency-safe): the reserved amount
    is deducted from the available contribution until committed/released.
    The check-and-insert sequence is synchronous (no awaits between the
    availability read and the insert), so it is atomic within the single
    asyncio process; the pending row makes the reservation durable.
    Returns dict with pending_id, refund_sol, fee_sol, net_sol,
    contribution_after. Raises ValueError if not enough available."""
    available = available_contribution(conn, telegram_id)
    refund_sol = available * fraction
    if refund_sol <= 0:
        raise ValueError("nessun contributo da rimborsare")
    fee = refund_sol * refund_fee_bps / 10_000.0
    net = refund_sol - fee
    if net <= 0:
        raise ValueError("importo troppo piccolo")
    pending_id = db.insert_pending_refund(conn, telegram_id,
                                          refund_sol, fee)
    return {
        "pending_id": pending_id,
        "refund_sol": refund_sol,
        "fee_sol": fee,
        "net_sol": net,
        "contribution_after": available - refund_sol,
    }


def commit_refund(conn, pending_id: int, signature: str | None = None) -> dict:
    """Convert a reservation into a ledger refund record (atomic)."""
    pending = db.get_pending_refund(conn, pending_id)
    if pending is None:
        raise ValueError("rimborso prenotato non trovato")
    db.record(conn, pending["telegram_id"], "refund",
              pending["amount_sol"], pending["fee_sol"], signature=signature)
    db.release_pending_refund(conn, pending_id)
    return pending


def release_refund(conn, pending_id: int) -> None:
    db.release_pending_refund(conn, pending_id)


def reconcile(conn, onchain_deposits: dict[str, float]) -> dict:
    """Compare ledger deposits (by tx signature) with on-chain incoming
    transfers. Returns {'missing_in_ledger': {sig: amount},
    'missing_onchain': [sigs]}."""
    ledger_sigs = db.ledger_deposit_sigs(conn)
    onchain_sigs = set(onchain_deposits)
    return {
        "missing_in_ledger": {s: onchain_deposits[s]
                              for s in onchain_sigs - ledger_sigs},
        "missing_onchain": sorted(ledger_sigs - onchain_sigs),
    }


def shares(conn) -> dict[int, float]:
    """SOL contribution per user (net of refunds)."""
    return {
        row["telegram_id"]: db.user_contribution(conn, row["telegram_id"])
        for row in conn.execute("SELECT DISTINCT telegram_id FROM ledger")
    }


def total_raised(conn) -> float:
    return db.total_raised(conn)
