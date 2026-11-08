"""Telegram handlers driven directly with fake Message/CallbackQuery
objects: refund flow, concurrency, wallet validation, UI consistency."""
from __future__ import annotations

import asyncio

import pytest
from conftest import ADMIN, USERS, FakeChain, Recorder, finding, make_call, make_message

from curvebot import db, service
from curvebot import handlers as h
from curvebot.keyboards import main_menu

U = USERS[0]
WALLET = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"


def _seed(conn, uid=U, sol=10.0, wallet=WALLET):
    db.set_wallet(conn, uid, wallet)
    service.record_deposit(conn, uid, sol)


async def _refund(uid, pct, cfg, conn, chain, rec):
    await h.cb_refund_amt(make_call(uid, f"refund:amt:{pct}", rec), cfg, conn)
    await h.cb_refund_go(make_call(uid, "refund:go", rec), cfg, conn, chain)


# ---------------- happy paths ----------------

def test_refund_full_flow(cfg, conn, chain, rec):
    _seed(conn)
    asyncio.run(_refund(U, 50, cfg, conn, chain, rec))
    assert chain.sent == [(WALLET, pytest.approx(5.0 * 0.997))]
    assert db.user_contribution(conn, U) == pytest.approx(5.0)
    assert "Rimborso completato" in rec.last


@pytest.mark.parametrize("pct", [25, 50, 75, 100])
def test_refund_each_button(cfg, conn, chain, rec, pct):
    _seed(conn, sol=4.0)
    asyncio.run(_refund(U, pct, cfg, conn, chain, rec))
    assert chain.sent[0][1] == pytest.approx(4.0 * pct / 100 * 0.997)
    assert db.user_contribution(conn, U) == pytest.approx(4.0 * (1 - pct / 100))


def test_refund_rpc_failure_records_nothing(cfg, conn, chain, rec):
    _seed(conn)
    chain.fail_send = True
    asyncio.run(_refund(U, 100, cfg, conn, chain, rec))
    assert db.user_contribution(conn, U) == pytest.approx(10.0)
    assert "fallito" in rec.last


def test_refund_closed_after_launch(cfg, conn, chain, rec):
    _seed(conn)
    db.set_meta(conn, "state", "launched")
    asyncio.run(_refund(U, 100, cfg, conn, chain, rec))
    assert chain.sent == []


def test_quote_is_single_use(cfg, conn, chain, rec):
    _seed(conn)
    asyncio.run(_refund(U, 25, cfg, conn, chain, rec))
    asyncio.run(h.cb_refund_go(make_call(U, "refund:go", rec), cfg, conn, chain))
    assert len(chain.sent) == 1
    assert "scaduta" in rec.alerts[-1]


def test_stranger_is_ignored(cfg, conn, chain, rec):
    _seed(conn)
    asyncio.run(_refund(999_999, 100, cfg, conn, chain, rec))
    asyncio.run(h.cmd_position(make_message(999_999, rec), cfg, conn))
    assert chain.sent == [] and rec.texts == []


def test_non_admin_cannot_launch(cfg, conn, chain, rec):
    _seed(conn)
    asyncio.run(h.cmd_launch(make_message(U, rec), cfg, conn))
    assert "amministratore" in rec.last
    asyncio.run(h.cb_launch_go(make_call(U, "launch:go", rec), cfg, conn, chain))
    assert "Solo l'admin." in rec.alerts


def test_launch_rejects_over_36(cfg, conn, rec):
    for uid in range(2000, 2037):
        _seed(conn, uid=uid, sol=0.1)
    asyncio.run(h.cmd_launch(make_message(ADMIN, rec), cfg, conn))
    assert "limite atomico" in rec.last


# ---------------- concurrency ----------------

def test_concurrent_refunds_pay_once(cfg, conn, rec):
    _seed(conn, sol=10.0)
    chain = FakeChain(balance_sol=100.0, send_delay=0.05)

    async def scenario():
        async def tap():
            await _refund(U, 100, cfg, conn, chain, Recorder())
        await asyncio.gather(tap(), tap())   # aiogram handles updates as tasks
    asyncio.run(scenario())
    paid = sum(a for _, a in chain.sent)
    assert paid <= 10.0 * 0.997 + 1e-9, f"paid {paid} SOL for a 10 SOL contribution"


@finding("refund allowed while run_launch is in flight (no 'launching' state): "
         "a contributor snapshotted for tokens can also take the SOL back")
def test_no_refund_while_launch_in_progress(cfg, conn, chain, rec, monkeypatch):
    _seed(conn)

    async def slow_launch(*a, **k):
        await asyncio.sleep(0.1)       # waiting for the bundle to land
        return {"mint": "M", "bundle_id": "B", "status": "confirmed",
                "dev_buy_sol": 9, "tokens_bought": 1, "distributed": 1,
                "team_tokens": 0, "dust_tokens": 0, "wallets": 1}
    import curvebot.launch as L
    monkeypatch.setattr(L, "run_launch", slow_launch)

    async def scenario():
        launch = asyncio.create_task(
            h.cb_launch_go(make_call(ADMIN, "launch:go", rec), cfg, conn, chain))
        await asyncio.sleep(0.01)
        await _refund(U, 100, cfg, conn, chain, rec)
        await launch
    asyncio.run(scenario())
    assert chain.sent == []


@finding("launch:go has no in-flight lock: two taps start two launches")
def test_double_tap_launch_runs_once(cfg, conn, chain, rec, monkeypatch):
    _seed(conn)
    calls = []

    async def slow_launch(*a, **k):
        calls.append(1)
        await asyncio.sleep(0.05)
        raise RuntimeError("stop here")
    import curvebot.launch as L
    monkeypatch.setattr(L, "run_launch", slow_launch)

    async def scenario():
        await asyncio.gather(
            h.cb_launch_go(make_call(ADMIN, "launch:go", rec), cfg, conn, chain),
            h.cb_launch_go(make_call(ADMIN, "launch:go", rec), cfg, conn, chain))
    asyncio.run(scenario())
    assert len(calls) == 1


# ---------------- wallet handling ----------------

def _setwallet(uid, arg, cfg, conn, rec):
    cmd = type("C", (), {"args": arg})()
    asyncio.run(h.cmd_setwallet(make_message(uid, rec), cmd, cfg, conn))


@pytest.mark.parametrize("bad", ["x" * 44, "0" * 40, "hello world this is not a key!!!!"])
def test_setwallet_validates_pubkey(cfg, conn, rec, bad):
    _setwallet(U, bad, cfg, conn, rec)
    assert db.get_wallet(conn, U) is None


@finding("two users can register the same wallet; deposits from it are "
         "credited to whichever row the monitor maps last")
def test_setwallet_rejects_wallet_already_taken(cfg, conn, rec):
    _setwallet(USERS[0], WALLET, cfg, conn, rec)
    _setwallet(USERS[1], WALLET, cfg, conn, rec)
    assert db.get_wallet(conn, USERS[1]) is None


@finding("wallet can be changed after depositing: later deposits from the "
         "original wallet are no longer recognised and are silently kept")
def test_setwallet_locked_after_first_deposit(cfg, conn, rec):
    _seed(conn)
    _setwallet(U, "7UX2i7SucgLMQcfZ75s3VXmZZY4YRUyJN9X1RgfMoDUi", cfg, conn, rec)
    assert db.get_wallet(conn, U) == WALLET


# ---------------- UI consistency ----------------

@finding("main menu shows '📤 Vendi' -> callback 'sell:open' with no handler "
         "(dead button; there is no Rimborso button)")
def test_every_menu_button_has_a_handler():
    datas = {b.callback_data for row in main_menu().inline_keyboard for b in row}
    handled = {"menu:howbuy", "menu:position", "menu:chart", "menu:info",
               "menu:back"}
    assert datas <= handled


def test_position_estimates_sum_to_total(cfg, conn):
    from curvebot.pump_curve import tokens_out_for_sol
    for uid, s in zip(USERS, (5.0, 10.0, 15.0)):
        _seed(conn, uid=uid, sol=s)
    est = sum(h._est_tokens(conn, cfg, db.user_contribution(conn, u)) for u in USERS)
    total = tokens_out_for_sol(db.total_raised(conn) * 0.9)
    assert est <= total * 1.001


def test_info_counts_zero_balance_users_as_participants(cfg, conn, chain, rec):
    """Documents: after a full refund the user still counts in /info."""
    _seed(conn)
    asyncio.run(_refund(U, 100, cfg, conn, chain, rec))
    text = h._fmt_info(cfg, conn, chain.pubkey, 0)
    assert "<code>1</code>" in text


def test_chart_callback_renders(cfg, conn, rec):
    _seed(conn)
    asyncio.run(h.cb_chart(make_call(U, "menu:chart", rec), cfg, conn))
    assert rec.photos == 1


def test_chart_handler_sends_input_file(cfg, conn):
    """aiogram rejects raw bytes: the handler must wrap the PNG."""
    from aiogram.types import BufferedInputFile
    sent = []

    async def answer_photo(photo, caption=None, reply_markup=None, **kw):
        sent.append(photo)
    call = make_call(U, "menu:chart", Recorder())
    call.message.answer_photo = answer_photo
    asyncio.run(h.cb_chart(call, cfg, conn))
    assert isinstance(sent[0], BufferedInputFile)
