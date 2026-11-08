"""Telegram handlers: commands + inline button callbacks (aiogram 3)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import db, service
from .chart import render_curve_png
from .curve import LinearCurve
from .keyboards import back_to_menu, main_menu, sell_amounts, sell_confirm

router = Router()

PENDING_SELLS: dict[int, dict] = {}


def _curve(cfg) -> LinearCurve:
    return LinearCurve(cfg.price_base_sol, cfg.slope_sol)


def _guard(cfg, user_id: int) -> bool:
    if not cfg.allowed_ids:
        return False
    return user_id in cfg.allowed_ids


def _fmt_info(cfg, conn, chain_pubkey: str, supply: float, sol: float) -> str:
    curve = _curve(cfg)
    return (
        f"🏦 <b>Jabba Curve</b>\n\n"
        f"💎 <b>Prezzo attuale:</b> <code>{curve.price_sol(supply):.10f}</code> SOL\n"
        f"📈 <b>Supply emesso:</b> <code>{supply:,.2f}</code>\n"
        f"💰 <b>SOL accumulati:</b> <code>{sol:.4f}</code>\n"
        f"🧾 <b>Fee:</b> {cfg.buy_fee_bps / 100:.2f}% buy · {cfg.sell_fee_bps / 100:.2f}% sell\n"
        f"⬇️ <b>Deposito minimo:</b> {cfg.min_deposit_sol} SOL\n\n"
        f"📬 <b>Indirizzo deposito:</b>\n<code>{chain_pubkey}</code>"
    )


async def _edit(call: CallbackQuery, text: str, markup=None):
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except Exception:
        await call.answer()


# --- commands ---

@router.message(Command("start", "help"))
async def cmd_start(message: Message, cfg, conn, chain):
    if not _guard(cfg, message.from_user.id):
        return
    supply = db.get_state(conn)["supply_tokens"]
    sol = await chain.balance_sol()
    await message.answer(
        _fmt_info(cfg, conn, chain.pubkey, supply, sol),
        reply_markup=main_menu(),
    )


@router.message(Command("menu"))
async def cmd_menu(message: Message, cfg, conn, chain):
    if not _guard(cfg, message.from_user.id):
        return
    supply = db.get_state(conn)["supply_tokens"]
    sol = await chain.balance_sol()
    await message.answer(_fmt_info(cfg, conn, chain.pubkey, supply, sol),
                         reply_markup=main_menu())


@router.message(Command("info"))
async def cmd_info(message: Message, cfg, conn, chain):
    if not _guard(cfg, message.from_user.id):
        return
    supply = db.get_state(conn)["supply_tokens"]
    sol = await chain.balance_sol()
    await message.answer(_fmt_info(cfg, conn, chain.pubkey, supply, sol))


@router.message(Command("price"))
async def cmd_price(message: Message, cfg, conn):
    if not _guard(cfg, message.from_user.id):
        return
    supply = db.get_state(conn)["supply_tokens"]
    await message.answer(
        f"💎 Prezzo: <code>{_curve(cfg).price_sol(supply):.10f}</code> SOL/token"
    )


@router.message(Command("position"))
async def cmd_position(message: Message, cfg, conn):
    if not _guard(cfg, message.from_user.id):
        return
    pos = db.user_position(conn, message.from_user.id)
    curve = _curve(cfg)
    supply = db.get_state(conn)["supply_tokens"]
    value = curve.sell_proceeds_sol(pos, supply, cfg.sell_fee_bps)[1]
    await message.answer(
        f"📊 <b>Posizione</b>\n\n"
        f"🪙 Token: <code>{pos:,.4f}</code>\n"
        f"💵 Valore se vendi ora (netto fee): <code>{value:.6f}</code> SOL"
    )


@router.message(Command("chart", "curva"))
async def cmd_chart(message: Message, cfg, conn):
    if not _guard(cfg, message.from_user.id):
        return
    supply = db.get_state(conn)["supply_tokens"]
    png = render_curve_png(cfg.price_base_sol, cfg.slope_sol, supply,
                           fee_pct=cfg.buy_fee_bps / 100)
    await message.answer_photo(png, caption="📈 <b>Bonding curve</b>")


@router.message(Command("setwallet"))
async def cmd_setwallet(message: Message, command: CommandObject, cfg, conn):
    if not _guard(cfg, message.from_user.id):
        return
    wallet = (command.args or "").strip()
    if not wallet or len(wallet) < 32:
        await message.answer("Uso: <code>/setwallet &lt;pubkey&gt;</code>")
        return
    db.set_wallet(conn, message.from_user.id, wallet)
    await message.answer(f"✅ Wallet registrato:\n<code>{wallet}</code>")


@router.message(Command("sell"))
async def cmd_sell_text(message: Message, command: CommandObject, cfg, conn):
    if not _guard(cfg, message.from_user.id):
        return
    pos = db.user_position(conn, message.from_user.id)
    if pos <= 0:
        await message.answer("Nessun token da vendere.")
        return
    await message.answer(
        f"📤 <b>Vendi token</b>\n\nHai <code>{pos:,.4f}</code> token.\n"
        f"Quanto vuoi vendere?",
        reply_markup=sell_amounts(),
    )


# --- callbacks ---

@router.callback_query(F.data == "menu:back")
async def cb_back(call: CallbackQuery, cfg, conn, chain):
    if not _guard(cfg, call.from_user.id):
        return await call.answer()
    supply = db.get_state(conn)["supply_tokens"]
    sol = await chain.balance_sol()
    await _edit(call, _fmt_info(cfg, conn, chain.pubkey, supply, sol), main_menu())


@router.callback_query(F.data == "menu:info")
async def cb_info(call: CallbackQuery, cfg, conn, chain):
    if not _guard(cfg, call.from_user.id):
        return await call.answer()
    supply = db.get_state(conn)["supply_tokens"]
    sol = await chain.balance_sol()
    await _edit(call, _fmt_info(cfg, conn, chain.pubkey, supply, sol), back_to_menu())
    await call.answer()


@router.callback_query(F.data == "menu:position")
async def cb_position(call: CallbackQuery, cfg, conn):
    if not _guard(cfg, call.from_user.id):
        return await call.answer()
    pos = db.user_position(conn, call.from_user.id)
    curve = _curve(cfg)
    supply = db.get_state(conn)["supply_tokens"]
    value = curve.sell_proceeds_sol(pos, supply, cfg.sell_fee_bps)[1]
    await _edit(call, (
        f"📊 <b>Posizione</b>\n\n"
        f"🪙 Token: <code>{pos:,.4f}</code>\n"
        f"💵 Valore se vendi ora (netto fee): <code>{value:.6f}</code> SOL"
    ), back_to_menu())
    await call.answer()


@router.callback_query(F.data == "menu:chart")
async def cb_chart(call: CallbackQuery, cfg, conn):
    if not _guard(cfg, call.from_user.id):
        return await call.answer()
    supply = db.get_state(conn)["supply_tokens"]
    png = render_curve_png(cfg.price_base_sol, cfg.slope_sol, supply,
                           fee_pct=cfg.buy_fee_bps / 100)
    await call.message.answer_photo(png, caption="📈 <b>Bonding curve</b>",
                                     reply_markup=back_to_menu())
    await call.answer()


@router.callback_query(F.data == "menu:howbuy")
async def cb_howbuy(call: CallbackQuery, cfg):
    if not _guard(cfg, call.from_user.id):
        return await call.answer()
    await _edit(call, (
        "💰 <b>Come comprare</b>\n\n"
        f"1️⃣ Registra il wallet: <code>/setwallet &lt;pubkey&gt;</code>\n"
        "2️⃣ Manda SOL all'indirizzo deposito (puoi copiarlo da ℹ️ Info)\n"
        f"3️⃣ Entro ~{cfg.poll_interval_s:.0f}s il bot accredita i token\n\n"
        f"🧾 Fee buy: <b>{cfg.buy_fee_bps / 100:.2f}%</b> · "
        f"minimo <b>{cfg.min_deposit_sol} SOL</b>"
    ), back_to_menu())
    await call.answer()


@router.callback_query(F.data == "sell:open")
async def cb_sell_open(call: CallbackQuery, cfg, conn):
    if not _guard(cfg, call.from_user.id):
        return await call.answer()
    pos = db.user_position(conn, call.from_user.id)
    if pos <= 0:
        await _edit(call, "😕 Nessun token da vendere.", back_to_menu())
        return await call.answer()
    await _edit(call, (
        f"📤 <b>Vendi token</b>\n\nHai <code>{pos:,.4f}</code> token.\n"
        f"Quanto vuoi vendere?"
    ), sell_amounts())
    await call.answer()


@router.callback_query(F.data.startswith("sell:amt:"))
async def cb_sell_amt(call: CallbackQuery, cfg, conn):
    if not _guard(cfg, call.from_user.id):
        return await call.answer()
    pct = int(call.data.split(":")[2])
    pos = db.user_position(conn, call.from_user.id)
    tokens = pos * pct / 100.0
    curve = _curve(cfg)
    try:
        result = service.sell(conn, curve, call.from_user.id, tokens,
                              cfg.sell_fee_bps)
    except ValueError as e:
        await call.answer(str(e), show_alert=True)
        return
    PENDING_SELLS[call.from_user.id] = result
    await _edit(call, (
        f"📤 <b>Conferma vendita</b>\n\n"
        f"🪙 Vendita: <code>{result['tokens']:,.4f}</code> token ({pct}%)\n"
        f"💵 Lordo: <code>{result['gross_sol']:.6f}</code> SOL\n"
        f"🧾 Fee ({cfg.sell_fee_bps / 100:.2f}%): <code>{result['fee_sol']:.6f}</code> SOL\n"
        f"✅ Ricevi: <b>{result['net_sol']:.6f} SOL</b>\n\n"
        f"Confermi?"
    ), sell_confirm())
    await call.answer()


@router.callback_query(F.data == "sell:go")
async def cb_sell_go(call: CallbackQuery, cfg, conn, chain):
    if not _guard(cfg, call.from_user.id):
        return await call.answer()
    result = PENDING_SELLS.pop(call.from_user.id, None)
    if result is None:
        await call.answer("Quotazione scaduta, ripeti la vendita.", show_alert=True)
        return
    wallet = db.get_wallet(conn, call.from_user.id)
    if not wallet:
        await _edit(call, "Registra prima il wallet: /setwallet <pubkey>", back_to_menu())
        return
    treasury = await chain.balance_sol()
    if treasury < result["net_sol"]:
        await _edit(call, "😕 Treasury SOL insufficiente, riprova più tardi.", back_to_menu())
        return
    try:
        sig = await chain.send_sol(wallet, result["net_sol"])
    except Exception:
        await _edit(call, "❌ Invio SOL fallito, riprova.", back_to_menu())
        return
    service.confirm_sell(conn, call.from_user.id, result["tokens"],
                         result["gross_sol"], result["fee_sol"], signature=sig)
    await _edit(call, (
        f"✅ <b>Vendita completata</b>\n\n"
        f"🪙 Venduti: <code>{result['tokens']:,.4f}</code> token\n"
        f"💸 Inviati: <b>{result['net_sol']:.6f} SOL</b> → <code>{wallet[:6]}…{wallet[-4:]}</code>\n"
        f"🧾 Fee: <code>{result['fee_sol']:.6f}</code> SOL\n"
        f"📊 Saldo residuo: <code>{result['position_after']:,.4f}</code> token"
    ), back_to_menu())
    await call.answer()


@router.callback_query(F.data == "sell:cancel")
async def cb_sell_cancel(call: CallbackQuery, cfg):
    PENDING_SELLS.pop(call.from_user.id, None)
    await _edit(call, "❌ Vendita annullata.", back_to_menu())
    await call.answer()


@router.message(Command("launch"))
async def cmd_launch(message: Message, cfg, conn):
    if not _guard(cfg, message.from_user.id):
        return
    admin_id = cfg.allowed_ids[0]
    if message.from_user.id != admin_id:
        await message.answer("Solo l'amministratore può lanciare.")
        return
    if db.get_meta(conn, "state") == "launched":
        mint = db.get_meta(conn, "mint")
        await message.answer(f"Presale già lanciato: pump.fun/coin/{mint}")
        return
    if not cfg.token_name or not cfg.token_symbol:
        await message.answer("Configura TOKEN_NAME e TOKEN_SYMBOL nel .env.")
        return
    positions = {
        row["telegram_id"]: db.user_position(conn, row["telegram_id"])
        for row in conn.execute("SELECT DISTINCT telegram_id FROM ledger")
    }
    positions = {u: p for u, p in positions.items() if p > 0}
    if not positions:
        await message.answer("Nessuna posizione nel presale, niente da lanciare.")
        return
    await message.answer(
        f"🚀 <b>LANCIO SU PUMP.FUN</b>\n\n"
        f"Token: <b>{cfg.token_name}</b> (${cfg.token_symbol})\n"
        f"Partecipanti: <b>{len(positions)}</b>\n"
        f"Dev buy: <b>{cfg.dev_buy_percent}%</b> del treasury\n"
        f"Quota team: <b>{cfg.team_share_bps / 100:.2f}%</b> (il resto agli utenti)\n\n"
        f"⚠️ Irreversibile, su mainnet. Confermi?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🚀 LANCIA ORA", callback_data="launch:go"),
            InlineKeyboardButton(text="❌ Annulla", callback_data="launch:cancel"),
        ]]),
    )


@router.callback_query(F.data == "launch:cancel")
async def cb_launch_cancel(call: CallbackQuery, cfg):
    if not _guard(cfg, call.from_user.id):
        return await call.answer()
    await _edit(call, "❌ Lancio annullato.")
    await call.answer()


@router.callback_query(F.data == "launch:go")
async def cb_launch_go(call: CallbackQuery, cfg, conn, chain):
    if not _guard(cfg, call.from_user.id):
        return await call.answer()
    if call.from_user.id != cfg.allowed_ids[0]:
        return await call.answer("Solo l'admin.", show_alert=True)
    if db.get_meta(conn, "state") == "launched":
        return await call.answer("Già lanciato.", show_alert=True)
    await call.answer("Lancio in corso…")
    import aiohttp

    from .launch import run_launch
    async with aiohttp.ClientSession() as http:
        try:
            report = await run_launch(chain, cfg, conn, http)
        except Exception as e:
            await call.message.answer(f"❌ Lancio fallito: {e}")
            return
    await call.message.answer(
        f"🚀 <b>LANCIATO!</b>\n\n"
        f"🪙 <code>{report['mint']}</code>\n"
        f"🔗 pump.fun/coin/{report['mint']}\n"
        f"⚛️ Bundle atomico: <code>{report['bundle_id']}</code>\n\n"
        f"Dev buy: {report['dev_buy_sol']:.4f} SOL\n"
        f"Token comprati: {report['tokens_bought']:,.0f}\n"
        f"Distribuiti: {report['distributed']:,.0f} a {report['wallets']} wallet\n"
        f"Team: {report['team_tokens']:,.0f}\n\n"
        f"I token sono nei wallet degli utenti. La curva è ora su pump.fun."
    )
