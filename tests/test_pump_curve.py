import pytest

from curvebot.pump_curve import DECIMALS, FEE_BPS, VSR0, VTR0, tokens_out_for_sol, ui_to_base


def test_zero():
    assert tokens_out_for_sol(0) == 0.0


def test_small_buy_reasonable():
    t = tokens_out_for_sol(1.0)
    # 1 SOL on a virgin curve buys a bit less than 1/31 of virtual reserves
    assert 25_000_000 < t < 40_000_000


def test_monotonic():
    prev = 0.0
    for sol in (0.1, 0.5, 1, 5, 20, 85):
        t = tokens_out_for_sol(sol)
        assert t > prev
        prev = t


def test_graduation_amount():
    """~85 SOL fills the curve to graduation (~793M of 1B tokens)."""
    t = tokens_out_for_sol(85.0)
    assert 750_000_000 < t < 850_000_000


def test_fee_applied():
    """With no fee the output would be strictly higher."""
    net_lamports = int(1.0 * 1e9 * (1 - FEE_BPS / 10_000))
    gross_out = VTR0 * (1.0 * 1e9) / (VSR0 + 1.0 * 1e9)
    net_out = VTR0 * net_lamports / (VSR0 + net_lamports)
    assert net_out < gross_out
    assert tokens_out_for_sol(1.0) == pytest.approx(net_out / 10 ** DECIMALS)


def test_ui_base_roundtrip():
    assert ui_to_base(123.456789) == 123_456_789
