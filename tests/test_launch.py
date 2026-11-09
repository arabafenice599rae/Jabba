import pytest

from curvebot.launch import CHUNK_SIZE, compute_distribution, plan_chunks


def weights_for(n):
    return {i: (i % 7 + 1) * 0.5 for i in range(1, n + 1)}


class TestInvariant:
    """Invariant: sum(shares) + team + dust == distributable <= tokens."""

    @pytest.mark.parametrize("n", [1, 15, 30, 36])
    def test_sum_equals_bought(self, n):
        tokens = 1_073_000_000 * 10**6 // 4
        dist, team, dust = compute_distribution(weights_for(n), tokens, 0)
        assert sum(a for _, a in dist) + team + dust == tokens
        assert team == 0 and dust >= 0

    def test_with_team_and_safety(self):
        tokens = 500_000_000 * 10**6
        dist, team, dust = compute_distribution(weights_for(10), tokens,
                                                team_bps=300, safety_bps=10)
        assert team == tokens * 300 // 10_000
        assert sum(a for _, a in dist) + dust == tokens * (10_000 - 300 - 10) // 10_000
        assert sum(a for _, a in dist) + team + dust <= tokens

    def test_deterministic(self):
        tokens = 123_456_789_123
        r1 = compute_distribution(weights_for(12), tokens, 0)
        r2 = compute_distribution(weights_for(12), tokens, 0)
        assert r1 == r2

    def test_proportional(self):
        dist, _, _ = compute_distribution({1: 60.0, 2: 30.0, 3: 10.0},
                                          1_000_000 * 10**6, 0)
        d = dict(dist)
        assert d[1] == 600_000 * 10**6
        assert d[2] == 300_000 * 10**6
        assert d[3] == 100_000 * 10**6

    def test_empty(self):
        assert compute_distribution({}, 100, 0) == ([], 0, 0)
        assert compute_distribution({1: 5}, 0, 0) == ([], 0, 0)


class TestChunking:
    @pytest.mark.parametrize("n,expected_chunks", [
        (1, 1), (9, 1), (10, 2), (15, 2), (30, 4), (36, 4),
    ])
    def test_chunking(self, n, expected_chunks):
        recipients = [(f"wallet{i}", 100) for i in range(n)]
        chunks = plan_chunks(recipients)
        assert len(chunks) == expected_chunks
        assert all(len(c) <= CHUNK_SIZE for c in chunks)
        assert sum(len(c) for c in chunks) == n

    @pytest.mark.parametrize("n", [37, 45, 46, 100])
    def test_rejects_over_capacity(self, n):
        recipients = [(f"wallet{i}", 100) for i in range(n)]
        with pytest.raises(ValueError, match="atomic"):
            plan_chunks(recipients)
