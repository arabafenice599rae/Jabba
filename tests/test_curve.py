import math

import pytest

from curvebot.curve import LinearCurve

P0 = 0.00001
M = 0.0000000005


@pytest.fixture
def curve():
    return LinearCurve(P0, M)


def test_price_at_zero_supply(curve):
    assert curve.price_sol(0) == P0


def test_price_increases_linearly(curve):
    assert curve.price_sol(100_000) == P0 + M * 100_000


def test_buy_roundtrip_consistency(curve):
    """Cost of buying x tokens must equal integral of price over [s, s+x]."""
    s, x = 123_000.0, 456.0
    cost = curve.cost_sol(x, s)
    analytic = P0 * x + M * s * x + M * x * x / 2
    assert cost == pytest.approx(analytic)


def test_tokens_out_inverts_cost(curve):
    s = 0.0
    amount = 1.5
    tokens = curve.tokens_out_for_sol(amount, s)
    assert tokens > 0
    assert curve.cost_sol(tokens, s) == pytest.approx(amount, rel=1e-9)


def test_tokens_out_at_nonzero_supply(curve):
    s, amount = 500_000.0, 2.0
    tokens = curve.tokens_out_for_sol(amount, s)
    assert tokens > 0
    assert curve.cost_sol(tokens, s) == pytest.approx(amount, rel=1e-9)


def test_sell_proceeds_match_buy_cost_at_same_supply(curve):
    """Selling x tokens bought when supply was s+x yields the same curve value."""
    s, x = 10_000.0, 500.0
    gross, net = curve.sell_proceeds_sol(x, s + x, sell_fee_bps=0)
    cost = curve.cost_sol(x, s)
    assert gross == pytest.approx(cost, rel=1e-9)
    assert net == pytest.approx(gross)


def test_sell_fee_applied(curve):
    gross, net = curve.sell_proceeds_sol(1_000.0, 50_000.0, sell_fee_bps=30)
    assert gross > net
    assert net == pytest.approx(gross * (1 - 30 / 10_000))


def test_sell_more_than_supply_clamped(curve):
    gross, _ = curve.sell_proceeds_sol(1_000.0, 100.0, sell_fee_bps=30)
    gross_100, _ = curve.sell_proceeds_sol(100.0, 100.0, sell_fee_bps=30)
    assert gross == pytest.approx(gross_100)


def test_zero_amounts(curve):
    assert curve.tokens_out_for_sol(0, 0) == 0
    assert curve.cost_sol(0, 0) == 0
    assert curve.sell_proceeds_sol(0, 100) == (0, 0)
    assert curve.sell_proceeds_sol(10, 0) == (0, 0)


def test_monotonic_price_with_buys(curve):
    supply = 0.0
    price_prev = curve.price_sol(supply)
    for _ in range(5):
        tokens = curve.tokens_out_for_sol(0.5, supply)
        supply += tokens
        price_now = curve.price_sol(supply)
        assert price_now > price_prev
        price_prev = price_now
