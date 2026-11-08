import pytest

from curvebot import db, service


@pytest.fixture
def conn(tmp_path):
    with db.connect(str(tmp_path / "test.db")) as c:
        yield c


def test_deposit_and_contribution(conn):
    service.record_deposit(conn, 1, 2.0, "sig1")
    service.record_deposit(conn, 1, 1.0, "sig2")
    assert db.user_contribution(conn, 1) == 3.0
    assert db.total_raised(conn) == 3.0


def test_refund_reduces_contribution(conn):
    service.record_deposit(conn, 1, 2.0)
    quote = service.refund_quote(conn, 1, 0.5, 30)  # 50%, 0.30% fee
    assert quote["refund_sol"] == pytest.approx(1.0)
    assert quote["fee_sol"] == pytest.approx(0.003)
    assert quote["net_sol"] == pytest.approx(0.997)
    service.confirm_refund(conn, 1, quote["refund_sol"], quote["fee_sol"])
    assert db.user_contribution(conn, 1) == pytest.approx(1.0)
    assert db.total_raised(conn) == pytest.approx(1.0)


def test_full_refund_zeroes_contribution(conn):
    service.record_deposit(conn, 1, 5.0)
    quote = service.refund_quote(conn, 1, 1.0, 30)
    service.confirm_refund(conn, 1, quote["refund_sol"], quote["fee_sol"])
    assert db.user_contribution(conn, 1) == 0.0
    with pytest.raises(ValueError):
        service.refund_quote(conn, 1, 1.0, 30)


def test_refund_rejects_empty(conn):
    with pytest.raises(ValueError):
        service.refund_quote(conn, 9, 1.0, 30)


def test_shares(conn):
    service.record_deposit(conn, 1, 3.0)
    service.record_deposit(conn, 2, 1.0)
    s = service.shares(conn)
    assert s == {1: 3.0, 2: 1.0}
