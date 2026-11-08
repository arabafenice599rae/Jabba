"""Off-chain token ledger operations on top of the curve, with buy/sell fees."""

from __future__ import annotations

from . import db
from .curve import LinearCurve


def buy(conn, curve: LinearCurve, telegram_id: int, amount_sol: float,
        buy_fee_bps: int, signature: str | None = None) -> dict:
    """Credit tokens for a SOL deposit. The buy fee is withheld first and
    stays in the treasury; only the net amount buys on the curve.

    Returns dict with fee_sol, net_sol, tokens.
    """
    fee_sol = amount_sol * buy_fee_bps / 10_000.0
    net_sol = amount_sol - fee_sol
    supply = db.get_state(conn)["supply_tokens"]
    tokens = curve.tokens_out_for_sol(net_sol, supply)
    if tokens <= 0:
        return {"fee_sol": 0.0, "net_sol": 0.0, "tokens": 0.0}
    db.record(conn, telegram_id, "buy", amount_sol, tokens, fee_sol=fee_sol,
              signature=signature)
    return {"fee_sol": fee_sol, "net_sol": net_sol, "tokens": tokens}


def sell(conn, curve: LinearCurve, telegram_id: int, tokens: float,
         sell_fee_bps: int) -> dict:
    """Debit tokens and compute the SOL refund. Does NOT move SOL on-chain:
    the caller sends `net_sol` to the user's wallet, then persists the record.

    Returns dict with gross_sol, fee_sol, net_sol, tokens, position_after,
    or raises ValueError if the user cannot sell that many tokens.
    """
    position = db.user_position(conn, telegram_id)
    if tokens <= 0:
        raise ValueError("importo non valido")
    if tokens > position:
        raise ValueError(f"saldo insufficiente: {position:,.4f} token")

    supply = db.get_state(conn)["supply_tokens"]
    gross, net = curve.sell_proceeds_sol(tokens, supply, sell_fee_bps)
    fee = gross - net
    if net <= 0:
        raise ValueError("importo troppo piccolo")

    return {
        "tokens": tokens,
        "gross_sol": gross,
        "fee_sol": fee,
        "net_sol": net,
        "position_after": position - tokens,
    }


def confirm_sell(conn, telegram_id: int, tokens: float, gross_sol: float,
                 fee_sol: float, signature: str | None = None) -> None:
    db.record(conn, telegram_id, "sell", gross_sol, tokens, fee_sol=fee_sol,
              signature=signature)
