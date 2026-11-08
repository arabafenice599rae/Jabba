import pytest

from curvebot.launch import compute_distribution


def test_distribution_proportional():
    positions = {1: 60.0, 2: 30.0, 3: 10.0}
    dist, team = compute_distribution(positions, 1000.0, 0)
    shares = dict(dist)
    assert shares[1] == pytest.approx(600.0)
    assert shares[2] == pytest.approx(300.0)
    assert shares[3] == pytest.approx(100.0)
    assert team == 0.0
    assert sum(shares.values()) == pytest.approx(1000.0)


def test_distribution_with_team_share():
    positions = {1: 50.0, 2: 50.0}
    dist, team = compute_distribution(positions, 1000.0, 500)  # 5%
    assert team == pytest.approx(50.0)
    shares = dict(dist)
    assert shares[1] == pytest.approx(475.0)
    assert shares[2] == pytest.approx(475.0)
    assert sum(shares.values()) + team == pytest.approx(1000.0)


def test_distribution_sums_exactly():
    positions = {i: i * 1.7 for i in range(1, 25)}
    tokens = 12345.678
    dist, team = compute_distribution(positions, tokens, 300)
    assert sum(a for _, a in dist) + team == pytest.approx(tokens, rel=1e-9)


def test_distribution_single_user():
    dist, team = compute_distribution({7: 100.0}, 500.0, 1000)  # 10%
    assert dict(dist)[7] == pytest.approx(450.0)
    assert team == pytest.approx(50.0)


def test_distribution_empty():
    assert compute_distribution({}, 100.0, 500) == ([], 0.0)
    assert compute_distribution({1: 10.0}, 0.0, 500) == ([], 0.0)
    assert compute_distribution({1: 0.0}, 100.0, 500) == ([], 0.0)
