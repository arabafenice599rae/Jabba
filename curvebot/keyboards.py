"""Inline keyboard layouts."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("💰 Come comprare", "menu:howbuy"), _btn("↩️ Rimborso", "refund:open")],
        [_btn("📊 Posizione", "menu:position"), _btn("📈 Curva", "menu:chart")],
        [_btn("ℹ️ Info", "menu:info")],
    ])



def back_to_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("↩️ Menu", "menu:back")],
    ])
