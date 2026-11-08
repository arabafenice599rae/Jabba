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


def test_contribution_sums_deposits_and_refunds(conn):
    db.record(conn, 1, "deposit", 1.0)
    db.record(conn, 1, "deposit", 0.5)
    db.record(conn, 1, "refund", 0.25, fee_sol=0.01)
    assert db.user_contribution(conn, 1) == pytest.approx(1.25)
    assert db.user_contribution(conn, 2) == 0.0


def test_total_raised_is_global(conn):
    db.record(conn, 1, "deposit", 1.0)
    db.record(conn, 2, "deposit", 1.0)
    db.record(conn, 2, "refund", 0.5)
    assert db.total_raised(conn) == pytest.approx(1.5)


def test_sig_dedup(conn):
    assert not db.sig_seen(conn, "sig1")
    db.mark_sig(conn, "sig1")
    assert db.sig_seen(conn, "sig1")
    db.mark_sig(conn, "sig1")
