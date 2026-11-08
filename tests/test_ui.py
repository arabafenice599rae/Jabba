import pytest

from curvebot.chart import render_curve_png
from curvebot.keyboards import back_to_menu, main_menu, sell_amounts, sell_confirm

P0 = 0.00001
M = 0.0000000005


def test_chart_png_valid():
    png = render_curve_png(P0, M, supply_tokens=50_000.0, fee_pct=0.30)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 10_000


def test_chart_png_zero_supply():
    png = render_curve_png(P0, M, supply_tokens=0.0)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_main_menu_layout():
    kb = main_menu()
    rows = kb.inline_keyboard
    assert len(rows[0]) == 2          # come comprare | vendi
    assert len(rows[1]) == 2          # posizione | curva
    assert len(rows[2]) == 1          # info
    datas = [b.callback_data for row in rows for b in row]
    assert set(datas) == {"menu:howbuy", "sell:open", "menu:position",
                          "menu:position", "menu:chart", "menu:info"} or \
        set(datas) == {"menu:howbuy", "sell:open", "menu:position",
                      "menu:chart", "menu:info"}


def test_sell_amounts_layout():
    kb = sell_amounts()
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert datas == ["sell:amt:25", "sell:amt:50", "sell:amt:75",
                     "sell:amt:100", "menu:back"]


def test_sell_confirm_layout():
    datas = [b.callback_data for row in sell_confirm().inline_keyboard for b in row]
    assert datas == ["sell:go", "sell:cancel"]


def test_back_button():
    datas = [b.callback_data for row in back_to_menu().inline_keyboard for b in row]
    assert datas == ["menu:back"]
