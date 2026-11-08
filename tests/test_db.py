import pytest

from curvebot import db


@pytest.fixture
def conn(tmp_path):
    with db.connect(str(tmp_path / "test.db")) as c:
        yield c


def test_set_and_get_wallet(conn):
    db.set_wallet(conn, 1, "WALLETAAA")
    assert db.get_wallet(conn, 1) == "WALLETAAA"
    db.set_wallet(conn, 1, "WALLETBBB")
    assert db.get_wallet(conn, 1) == "WALLETBBB"


def test_position_sums_buys_and_sells(conn):
    db.record(conn, 1, "buy", 1.0, 100.0)
    db.record(conn, 1, "buy", 1.0, 50.0)
    db.record(conn, 1, "sell", 0.5, 30.0, fee_sol=0.01)
    assert db.user_position(conn, 1) == 120.0
    assert db.user_position(conn, 2) == 0.0


def test_supply_is_global(conn):
    db.record(conn, 1, "buy", 1.0, 100.0)
    db.record(conn, 2, "buy", 1.0, 100.0)
    assert db.get_state(conn)["supply_tokens"] == 200.0


def test_sig_dedup(conn):
    assert not db.sig_seen(conn, "sig1")
    db.mark_sig(conn, "sig1")
    assert db.sig_seen(conn, "sig1")
    db.mark_sig(conn, "sig1")
