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
    """Atomically reserve a refund. Check-and-insert run in ONE explicit
    BEGIN IMMEDIATE transaction (atomic across connections/processes);
    the pending row makes the reservation durable across restarts.
    Returns dict with pending_id, refund_sol, fee_sol, net_sol,
    contribution_after. Raises ValueError if not enough available."""
    fraction = max(0.0, min(1.0, fraction))
    if not 0 <= refund_fee_bps <= 10_000:
        raise ValueError("fee non valida (fuori range 0-100%)")
    conn.execute("BEGIN IMMEDIATE")
    try:
        available = available_contribution(conn, telegram_id)
        refund_sol = available * fraction
        if refund_sol <= 0:
            raise ValueError("nessun contributo da rimborsare")
        fee = refund_sol * refund_fee_bps / 10_000.0
        net = refund_sol - fee
        if int(round(net * 1_000_000_000)) <= 0:
            raise ValueError("importo troppo piccolo (sub-lamport)")
        pending_id = db.insert_pending_refund(conn, telegram_id,
                                              refund_sol, fee)
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
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


def mark_refund_unknown(conn, pending_id: int) -> None:
    """Uncertain outcome (e.g. RPC timeout after submission): the
    reservation STAYS HELD and the refund enters 'unknown' state. A new
    refund is only possible after reconciliation resolves it."""
    db.set_refund_status(conn, pending_id, "unknown")


async def resolve_unknown_refunds(conn, chain, grace_seconds: int = 600
                                   ) -> list[dict]:
    """Resolve 'unknown' refunds against the chain: if a matching SOL
    transfer from the treasury to the user's wallet is found (finalized),
    commit the refund with its signature; if none is found after the
    grace period, release the reservation. Returns resolution log."""
    resolutions = []
    unknowns = db.list_unknown_refunds(conn)
    for pending in unknowns:
        wallet = db.get_wallet(conn, pending["telegram_id"])
        net = pending["amount_sol"] - pending["fee_sol"]
        landed = None
        if wallet:
            transfers = await chain.transfers_to_wallet(wallet)
            matches = [t for t in transfers
                       if abs(t["amount_sol"] - net) < 1e-9]
            if matches:
                landed = matches[0]
        if landed is not None:
            db.record(conn, pending["telegram_id"], "refund",
                      pending["amount_sol"], pending["fee_sol"],
                      signature=landed["signature"])
            db.release_pending_refund(conn, pending["id"])
            resolutions.append({"id": pending["id"], "outcome": "committed",
                                "signature": landed["signature"]})
        else:
            from datetime import datetime, timezone
            try:
                created = datetime.fromisoformat(pending["created_at"])
                age = (datetime.now(timezone.utc) - created).total_seconds()
            except Exception:
                age = grace_seconds + 1
            if age > grace_seconds:
                db.release_pending_refund(conn, pending["id"])
                resolutions.append({"id": pending["id"],
                                    "outcome": "released"})
    return resolutions


def reconcile(conn, onchain_deposits: dict[str, float]) -> dict:
    """Six-category ledger <-> chain reconciliation. NEVER auto-corrects:
    reports only; fixes require a controlled procedure.

    Categories: missing_in_ledger (finalized deposit not credited),
    unverifiable_onchain (ledger sig outside the on-chain window — not
    declared missing), amount_mismatch, duplicate_ledger,
    unknown_refunds (held), refund_sigs_unresolved (no signature).
    """
    ledger = db.ledger_deposits(conn)
    onchain = onchain_deposits
    ledger_sigs = set(ledger)
    onchain_sigs = set(onchain)
    common = ledger_sigs & onchain_sigs
    return {
        "missing_in_ledger": {s: onchain[s] for s in onchain_sigs - ledger_sigs},
        "unverifiable_onchain": sorted(ledger_sigs - onchain_sigs),
        "amount_mismatch": {s: (ledger[s], onchain[s]) for s in common
                            if abs(ledger[s] - onchain[s]) > 1e-9},
        "duplicate_ledger": db.duplicate_deposit_sigs(conn),
        "unknown_refunds": [r["id"] for r in db.list_unknown_refunds(conn)],
        "refund_sigs_unresolved": [
            r["id"] for r in conn.execute(
                "SELECT id FROM ledger WHERE kind = 'refund' "
                "AND signature IS NULL").fetchall()],
    }


def shares(conn) -> dict[int, float]:
    """SOL contribution per user (net of refunds)."""
    return {
        row["telegram_id"]: db.user_contribution(conn, row["telegram_id"])
        for row in conn.execute("SELECT DISTINCT telegram_id FROM ledger")
    }


def total_raised(conn) -> float:
    return db.total_raised(conn)
