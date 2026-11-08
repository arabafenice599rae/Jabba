import pytest

from curvebot import db, service
from curvebot.curve import LinearCurve

BUY_FEE_BPS = 30
SELL_FEE_BPS = 30


@pytest.fixture
def conn(tmp_path):
    with db.connect(str(tmp_path / "test.db")) as c:
        yield c


@pytest.fixture
def curve():
    return LinearCurve(0.00001, 0.0000000005)


def test_buy_withholds_fee(conn, curve):
    result = service.buy(conn, curve, 1, 1.0, BUY_FEE_BPS)
    assert result["fee_sol"] == pytest.approx(0.003)
    assert result["net_sol"] == pytest.approx(0.997)
    assert result["tokens"] > 0
    assert db.user_position(conn, 1) == pytest.approx(result["tokens"])


def test_buy_fee_reduces_tokens(conn, curve, tmp_path):
    with_fee = service.buy(conn, curve, 1, 1.0, BUY_FEE_BPS)["tokens"]
    with db.connect(str(tmp_path / "nofee.db")) as conn2:
        no_fee = service.buy(conn2, curve, 1, 1.0, 0)["tokens"]
    assert with_fee < no_fee


def test_buy_increases_price_for_next_buyer(conn, curve):
    t1 = service.buy(conn, curve, 1, 1.0, BUY_FEE_BPS)["tokens"]
    t2 = service.buy(conn, curve, 2, 1.0, BUY_FEE_BPS)["tokens"]
    assert t2 < t1


def test_sell_debits_and_computes_refund(conn, curve):
    service.buy(conn, curve, 1, 1.0, BUY_FEE_BPS)
    pos = db.user_position(conn, 1)
    result = service.sell(conn, curve, 1, pos / 2, SELL_FEE_BPS)
    assert result["fee_sol"] > 0
    assert result["net_sol"] == pytest.approx(result["gross_sol"] - result["fee_sol"])
    assert result["position_after"] == pytest.approx(pos / 2)
    service.confirm_sell(conn, 1, result["tokens"], result["gross_sol"], result["fee_sol"])
    assert db.user_position(conn, 1) == pytest.approx(pos / 2)
    assert db.get_state(conn)["supply_tokens"] == pytest.approx(pos / 2)


def test_sell_rejects_more_than_position(conn, curve):
    service.buy(conn, curve, 1, 1.0, BUY_FEE_BPS)
    pos = db.user_position(conn, 1)
    with pytest.raises(ValueError):
        service.sell(conn, curve, 1, pos + 1, SELL_FEE_BPS)


def test_sell_rejects_zero(conn, curve):
    with pytest.raises(ValueError):
        service.sell(conn, curve, 1, 0, SELL_FEE_BPS)


def test_full_roundtrip_preserves_supply(conn, curve):
    service.buy(conn, curve, 1, 1.0, BUY_FEE_BPS)
    pos = db.user_position(conn, 1)
    result = service.sell(conn, curve, 1, pos, SELL_FEE_BPS)
    service.confirm_sell(conn, 1, result["tokens"], result["gross_sol"], result["fee_sol"])
    assert db.user_position(conn, 1) == 0
    assert db.get_state(conn)["supply_tokens"] == pytest.approx(0)


def test_roundtrip_user_recovers_less_than_deposit(conn, curve):
    """Deposit 1 SOL, sell everything back: net received must be deposit minus
    buy fee and sell fee."""
    deposit = 1.0
    service.buy(conn, curve, 1, deposit, BUY_FEE_BPS)
    pos = db.user_position(conn, 1)
    result = service.sell(conn, curve, 1, pos, SELL_FEE_BPS)
    expected_net = (deposit * (1 - BUY_FEE_BPS / 10_000)) * (1 - SELL_FEE_BPS / 10_000)
    assert result["net_sol"] == pytest.approx(expected_net, rel=1e-9)
    assert result["net_sol"] < deposit
