"""Telegram handlers: presale (flat, SOL-based) + atomic launch."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import db, service
from .keyboards import back_to_menu, main_menu
from .pump_curve import tokens_out_for_sol

router = Router()

PENDING_REFUNDS: dict[int, dict] = {}


def _guard(cfg, user_id: int) -> bool:
    if not cfg.allowed_ids:
        return False
    return user_id in cfg.allowed_ids


def _est_tokens(conn, cfg, contribution: float) -> float:
    """Estimated tokens at launch for a contribution, given treasury size."""
    raised = db.total_raised(conn)
    if raised <= 0:
        return 0.0
    dev_buy = raised * (100.0 - cfg.team_keep_percent) / 100.0
    expected = tokens_out_for_sol(dev_buy)
    user_share = tokens_out_for_sol(dev_buy * contribution / raised)
    return user_share * (1 - cfg.team_share_bps / 10_000.0)


def _fmt_info(cfg, conn, chain_pubkey: str, sol: float) -> str:
    raised = db.total_raised(conn)
    n = len(service.shares(conn))
    est = tokens_out_for_sol(raised * (100.0 - cfg.team_keep_percent) / 100.0) \
        if raised > 0 else 0.0
    return (
        f"🏦 <b>Jabba Presale</b>\n\n"
        f"💰 <b>Raccolto:</b> <code>{raised:.4f} SOL</code>\n"
        f"👥 <b>Partecipanti:</b> <code>{n}</code>\n"
        f"🪙 <b>Token stimati al lancio:</b> <code>{est:,.0f}</code>\n"
        f"🧾 <b>Fee rimborso:</b> {cfg.refund_fee_bps / 100:.2f}%\n"
        f"🧾 <b>Quota team al lancio:</b> {cfg.team_share_bps / 100:.2f}%\n"
        f"⬇️ <b>Deposito minimo:</b> {cfg.min_deposit_sol} SOL\n\n"
        f"📬 <b>Indirizzo deposito:</b>\n<code>{chain_pubkey}</code>\n\n"
        f"💰 <b>Treasury:</b> <code>{sol:.4f} SOL</code>"
    )


async def _edit(call: CallbackQuery, text: str, markup=None):
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except Exception:
        await call.answer()


# --- commands ---

@router.message(Command("start", "help", "menu"))
async def cmd_start(message: Message, cfg, conn, chain):
    if not _guard(cfg, message.from_user.id):
        return
    sol = await chain.balance_sol()
    await message.answer(_fmt_info(cfg, conn, chain.pubkey, sol),
                         reply_markup=main_menu())


@router.message(Command("info"))
async def cmd_info(message: Message, cfg, conn, chain):
    if not _guard(cfg, message.from_user.id):
        return
    sol = await chain.balance_sol()
    await message.answer(_fmt_info(cfg, conn, chain.pubkey, sol))


@router.message(Command("position"))
async def cmd_position(message: Message, cfg, conn):
    if not _guard(cfg, message.from_user.id):
        return
    c = db.user_contribution(conn, message.from_user.id)
    raised = db.total_raised(conn)
    share = c / raised * 100 if raised > 0 else 0
    est = _est_tokens(conn, cfg, c)
    await message.answer(
        f"📊 <b>Posizione</b>\n\n"
        f"💰 Contribuito: <code>{c:.4f} SOL</code>\n"
        f"📈 Quota: <code>{share:.2f}%</code> del presale\n"
        f"🪙 Token stimati al lancio: <code>{est:,.0f}</code>"
    )


@router.message(Command("refund"))
async def cmd_refund(message: Message, cfg, conn):
    if not _guard(cfg, message.from_user.id):
        return
    c = db.user_contribution(conn, message.from_user.id)
    if c <= 0:
        await message.answer("Nessun contributo da rimborsare.")
        return
    await message.answer(
        f"↩️ <b>Rimborso</b>\n\nHai contribuito <code>{c:.4f} SOL</code>.\n"
        f"Quanto vuoi rimborsare? (fee {cfg.refund_fee_bps / 100:.2f}%)",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="25%", callback_data="refund:amt:25"),
             InlineKeyboardButton(text="50%", callback_data="refund:amt:50")],
            [InlineKeyboardButton(text="75%", callback_data="refund:amt:75"),
             InlineKeyboardButton(text="100%", callback_data="refund:amt:100")],
            [InlineKeyboardButton(text="↩️ Indietro", callback_data="menu:back")],
        ]),
    )


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


@router.message(Command("launch"))
async def cmd_launch(message: Message, cfg, conn):
    if not _guard(cfg, message.from_user.id):
        return
    if message.from_user.id != cfg.allowed_ids[0]:
        await message.answer("Solo l'amministratore può lanciare.")
        return
    if db.get_meta(conn, "state") == "launched":
        mint = db.get_meta(conn, "mint")
        await message.answer(f"Presale già lanciato: pump.fun/coin/{mint}")
        return
    if not cfg.token_name or not cfg.token_symbol:
        await message.answer("Configura TOKEN_NAME e TOKEN_SYMBOL nel .env.")
        return
    contributions = {u: c for u, c in service.shares(conn).items() if c > 0}
    if not contributions:
        await message.answer("Nessun contributo nel presale, niente da lanciare.")
        return
    raised = db.total_raised(conn)
    await message.answer(
        f"🚀 <b>LANCIO SU PUMP.FUN</b>\n\n"
        f"Token: <b>{cfg.token_name}</b> (${cfg.token_symbol})\n"
        f"Raccolto: <b>{raised:.4f} SOL</b> ({len(contributions)} partecipanti)\n"
        f"Team trattiene: <b>{cfg.team_keep_percent:.0f}%</b> dei SOL\n"
        f"Dev buy: <b>{100 - cfg.team_keep_percent:.0f}%</b> dei SOL → pump.fun\n"
        f"Token: <b>100% agli utenti</b> (∝ SOL versati)\n"
        f"Bundle atomico: create + buy + distribuzione nello stesso slot\n\n"
        f"⚠️ Irreversibile, su mainnet. Confermi?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🚀 LANCIA ORA", callback_data="launch:go"),
            InlineKeyboardButton(text="❌ Annulla", callback_data="launch:cancel"),
        ]]),
    )


# --- callbacks ---

@router.callback_query(F.data == "menu:back")
async def cb_back(call: CallbackQuery, cfg, conn, chain):
    if not _guard(cfg, call.from_user.id):
        return await call.answer()
    sol = await chain.balance_sol()
    await _edit(call, _fmt_info(cfg, conn, chain.pubkey, sol), main_menu())


@router.callback_query(F.data == "menu:info")
async def cb_info(call: CallbackQuery, cfg, conn, chain):
    if not _guard(cfg, call.from_user.id):
        return await call.answer()
    sol = await chain.balance_sol()
    await _edit(call, _fmt_info(cfg, conn, chain.pubkey, sol), back_to_menu())
    await call.answer()


@router.callback_query(F.data == "menu:position")
async def cb_position(call: CallbackQuery, cfg, conn):
    if not _guard(cfg, call.from_user.id):
        return await call.answer()
    c = db.user_contribution(conn, call.from_user.id)
    raised = db.total_raised(conn)
    share = c / raised * 100 if raised > 0 else 0
    est = _est_tokens(conn, cfg, c)
    await _edit(call, (
        f"📊 <b>Posizione</b>\n\n"
        f"💰 Contribuito: <code>{c:.4f} SOL</code>\n"
        f"📈 Quota: <code>{share:.2f}%</code> del presale\n"
        f"🪙 Token stimati al lancio: <code>{est:,.0f}</code>"
    ), back_to_menu())
    await call.answer()


@router.callback_query(F.data == "menu:howbuy")
async def cb_howbuy(call: CallbackQuery, cfg):
    if not _guard(cfg, call.from_user.id):
        return await call.answer()
    await _edit(call, (
        "💰 <b>Come partecipare</b>\n\n"
        f"1️⃣ Registra il wallet: <code>/setwallet &lt;pubkey&gt;</code>\n"
        "2️⃣ Manda SOL all'indirizzo deposito (da ℹ️ Info)\n"
        f"3️⃣ Entro ~{cfg.poll_interval_s:.0f}s il contributo è registrato\n\n"
        f"Al lancio ricevi token proporzionali ai tuoi SOL.\n"
        f"🧾 Rimborso libero prima del lancio: fee {cfg.refund_fee_bps / 100:.2f}%\n"
        f"⬇️ Minimo <b>{cfg.min_deposit_sol} SOL</b>"
    ), back_to_menu())
    await call.answer()


@router.callback_query(F.data == "menu:chart")
async def cb_chart(call: CallbackQuery, cfg, conn):
    if not _guard(cfg, call.from_user.id):
        return await call.answer()
    from .chart import render_pump_curve_png
    raised = db.total_raised(conn)
    png = render_pump_curve_png(dev_buy_sol=raised * (100.0 - cfg.team_keep_percent) / 100.0)
    await call.message.answer_photo(
        png, caption="📈 <b>Curva pump.fun attesa</b> (dev buy al lancio)",
        reply_markup=back_to_menu())
    await call.answer()


@router.callback_query(F.data.startswith("refund:amt:"))
async def cb_refund_amt(call: CallbackQuery, cfg, conn):
    if not _guard(cfg, call.from_user.id):
        return await call.answer()
    if db.get_meta(conn, "state") == "launched":
        await call.answer("Presale già lanciato: rimborsi chiusi.",
                          show_alert=True)
        return
    pct = int(call.data.split(":")[2])
    try:
        quote = service.refund_quote(conn, call.from_user.id, pct / 100.0,
                                     cfg.refund_fee_bps)
    except ValueError as e:
        await call.answer(str(e), show_alert=True)
        return
    PENDING_REFUNDS[call.from_user.id] = quote
    await _edit(call, (
        f"↩️ <b>Conferma rimborso</b>\n\n"
        f"💰 Rimborso: <code>{quote['refund_sol']:.4f} SOL</code> ({pct}%)\n"
        f"🧾 Fee ({cfg.refund_fee_bps / 100:.2f}%): <code>{quote['fee_sol']:.6f} SOL</code>\n"
        f"✅ Ricevi: <b>{quote['net_sol']:.6f} SOL</b>\n"
        f"📉 Contributo residuo: <code>{quote['contribution_after']:.4f} SOL</code>\n\n"
        f"Confermi?"
    ), InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Conferma rimborso", callback_data="refund:go"),
        InlineKeyboardButton(text="❌ Annulla", callback_data="refund:cancel"),
    ]]))
    await call.answer()


@router.callback_query(F.data == "refund:go")
async def cb_refund_go(call: CallbackQuery, cfg, conn, chain):
    if not _guard(cfg, call.from_user.id):
        return await call.answer()
    quote = PENDING_REFUNDS.pop(call.from_user.id, None)
    if quote is None:
        await call.answer("Quotazione scaduta, ripeti.", show_alert=True)
        return
    if db.get_meta(conn, "state") == "launched":
        await call.answer("Presale già lanciato.", show_alert=True)
        return
    wallet = db.get_wallet(conn, call.from_user.id)
    if not wallet:
        await _edit(call, "Registra prima il wallet: /setwallet <pubkey>",
                    back_to_menu())
        return
    treasury = await chain.balance_sol()
    if treasury < quote["net_sol"]:
        await _edit(call, "😕 Treasury SOL insufficiente, riprova più tardi.",
                    back_to_menu())
        return
    try:
        sig = await chain.send_sol(wallet, quote["net_sol"])
    except Exception:
        await _edit(call, "❌ Invio SOL fallito, riprova.", back_to_menu())
        return
    service.confirm_refund(conn, call.from_user.id, quote["refund_sol"],
                           quote["fee_sol"], signature=sig)
    await _edit(call, (
        f"✅ <b>Rimborso completato</b>\n\n"
        f"💸 Inviati: <b>{quote['net_sol']:.6f} SOL</b> → <code>{wallet[:6]}…{wallet[-4:]}</code>\n"
        f"🧾 Fee: <code>{quote['fee_sol']:.6f} SOL</code>\n"
        f"📉 Contributo residuo: <code>{quote['contribution_after']:.4f} SOL</code>"
    ), back_to_menu())
    await call.answer()


@router.callback_query(F.data == "refund:cancel")
async def cb_refund_cancel(call: CallbackQuery, cfg):
    PENDING_REFUNDS.pop(call.from_user.id, None)
    await _edit(call, "❌ Rimborso annullato.", back_to_menu())
    await call.answer()


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
