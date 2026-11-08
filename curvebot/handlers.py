"""Telegram command handlers (aiogram 3). Token is off-chain (DB only)."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from . import db, service
from .curve import LinearCurve

router = Router()


def _curve(cfg) -> LinearCurve:
    return LinearCurve(cfg.price_base_sol, cfg.slope_sol)


def _guard(cfg, message: Message) -> bool:
    if not cfg.allowed_ids:
        return False
    return message.from_user is not None and message.from_user.id in cfg.allowed_ids


async def _reply(message: Message, text: str):
    await message.reply(text, parse_mode=None)


@router.message(Command("start", "help"))
async def cmd_start(message: Message, cfg):
    if not _guard(cfg, message):
        return
    await _reply(message, (
        "Comandi:\n"
        "/setwallet <pubkey> — registra il wallet per depositi e rimborsi\n"
        "/info — indirizzo deposito, prezzo, supply, fee\n"
        "/price — prezzo attuale\n"
        "/position — token posseduti (ledger off-chain)\n"
        "/sell <importo|%> — vendi token: ricevi SOL meno la fee\n"
        "\nBuy: manda SOL all'indirizzo deposito dal wallet registrato; "
        "il saldo token viene accreditato qui nel bot."
    ))


@router.message(Command("setwallet"))
async def cmd_setwallet(message: Message, command: CommandObject, cfg, conn):
    if not _guard(cfg, message):
        return
    wallet = (command.args or "").strip()
    if not wallet or len(wallet) < 32:
        await _reply(message, "Uso: /setwallet <pubkey>")
        return
    db.set_wallet(conn, message.from_user.id, wallet)
    await _reply(message, f"Wallet registrato: {wallet}")


@router.message(Command("info"))
async def cmd_info(message: Message, cfg, conn, chain):
    if not _guard(cfg, message):
        return
    curve = _curve(cfg)
    supply = db.get_state(conn)["supply_tokens"]
    sol = await chain.balance_sol()
    await _reply(message, (
        f"Deposito: {chain.pubkey}\n"
        f"Prezzo attuale: {curve.price_sol(supply):.10f} SOL\n"
        f"Supply emesso: {supply:,.2f}\n"
        f"SOL accumulati: {sol:.4f}\n"
        f"Fee buy: {cfg.buy_fee_bps / 100:.2f}%\n"
        f"Fee vendita: {cfg.sell_fee_bps / 100:.2f}%\n"
        f"Deposito minimo: {cfg.min_deposit_sol} SOL"
    ))


@router.message(Command("price"))
async def cmd_price(message: Message, cfg, conn):
    if not _guard(cfg, message):
        return
    curve = _curve(cfg)
    supply = db.get_state(conn)["supply_tokens"]
    await _reply(message, f"Prezzo: {curve.price_sol(supply):.10f} SOL/token")


@router.message(Command("position"))
async def cmd_position(message: Message, cfg, conn):
    if not _guard(cfg, message):
        return
    pos = db.user_position(conn, message.from_user.id)
    await _reply(message, f"Token: {pos:,.4f}")


@router.message(Command("sell"))
async def cmd_sell(message: Message, command: CommandObject, cfg, conn, chain):
    if not _guard(cfg, message):
        return
    args = (command.args or "").strip()
    if not args:
        await _reply(message, "Uso: /sell <importo token> oppure /sell 50%")
        return
    wallet = db.get_wallet(conn, message.from_user.id)
    if not wallet:
        await _reply(message, "Prima registra il wallet con /setwallet <pubkey>")
        return

    pos = db.user_position(conn, message.from_user.id)
    if pos <= 0:
        await _reply(message, "Nessun token da vendere.")
        return

    try:
        if args.endswith("%"):
            pct = max(0.0, min(100.0, float(args[:-1])))
            tokens = pos * pct / 100.0
        else:
            tokens = float(args)
    except ValueError:
        await _reply(message, "Importo non valido.")
        return
    if tokens <= 0:
        await _reply(message, "Importo non valido.")
        return

    curve = _curve(cfg)
    try:
        result = service.sell(conn, curve, message.from_user.id, tokens,
                              cfg.sell_fee_bps)
    except ValueError as e:
        await _reply(message, str(e))
        return

    treasury = await chain.balance_sol()
    if treasury < result["net_sol"]:
        await _reply(message, "Treasury SOL insufficiente, riprova più tardi.")
        return

    try:
        sig = await chain.send_sol(wallet, result["net_sol"])
    except Exception:
        await _reply(message, "Invio SOL fallito, riprova.")
        return

    service.confirm_sell(
        conn, message.from_user.id, result["tokens"],
        result["gross_sol"], result["fee_sol"], signature=sig,
    )
    await _reply(message, (
        f"Venduti {result['tokens']:,.4f} token\n"
        f"Lordo: {result['gross_sol']:.6f} SOL\n"
        f"Fee ({cfg.sell_fee_bps / 100:.2f}%): {result['fee_sol']:.6f} SOL\n"
        f"Inviati: {result['net_sol']:.6f} SOL → {wallet}\n"
        f"Saldo residuo: {result['position_after']:,.4f} token"
    ))
