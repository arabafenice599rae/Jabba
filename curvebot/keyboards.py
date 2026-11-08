"""Inline keyboard layouts."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("💰 Come comprare", "menu:howbuy"), _btn("📤 Vendi", "sell:open")],
        [_btn("📊 Posizione", "menu:position"), _btn("📈 Curva", "menu:chart")],
        [_btn("ℹ️ Info", "menu:info")],
    ])


def sell_amounts() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("25%", "sell:amt:25"), _btn("50%", "sell:amt:50")],
        [_btn("75%", "sell:amt:75"), _btn("100%", "sell:amt:100")],
        [_btn("↩️ Indietro", "menu:back")],
    ])


def sell_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("✅ Conferma vendita", "sell:go"), _btn("❌ Annulla", "sell:cancel")],
    ])


def back_to_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("↩️ Menu", "menu:back")],
    ])
