
from curvebot.chart import render_pump_curve_png
from curvebot.keyboards import back_to_menu, main_menu


def test_chart_png_valid():
    png = render_pump_curve_png(dev_buy_sol=5.0)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 10_000


def test_chart_png_zero_dev_buy():
    png = render_pump_curve_png(dev_buy_sol=0.0)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_main_menu_layout():
    kb = main_menu()
    rows = kb.inline_keyboard
    assert len(rows[0]) == 2          # come comprare | rimborso
    assert len(rows[1]) == 2          # posizione | curva
    assert len(rows[2]) == 1          # info
    datas = [b.callback_data for row in rows for b in row]
    assert set(datas) == {"menu:howbuy", "refund:open", "menu:position",
                          "menu:chart", "menu:info"}


def test_back_button():
    datas = [b.callback_data for row in back_to_menu().inline_keyboard for b in row]
    assert datas == ["menu:back"]
