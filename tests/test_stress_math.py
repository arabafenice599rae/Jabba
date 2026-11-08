"""Property-based stress on the pure math: pump.fun curve and the
proportional token distribution."""
from __future__ import annotations

from fractions import Fraction

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from conftest import finding
from curvebot import pump_curve as pc
from curvebot.launch import (CHUNK_SIZE, MAX_PARTICIPANTS, compute_distribution,
                             plan_chunks)

SETTINGS = settings(max_examples=1500, deadline=None,
                    suppress_health_check=[HealthCheck.too_slow])
sol = st.floats(min_value=1e-9, max_value=80.0, allow_nan=False)


# ---------------- curve ----------------

@SETTINGS
@given(sol)
def test_curve_output_integer_and_bounded(s):
    out = pc.tokens_out_for_sol(s)
    assert isinstance(out, int)
    assert 0 <= out < pc.VTR0


@SETTINGS
@given(sol, sol)
def test_curve_monotonic(a, b):
    lo, hi = sorted((a, b))
    assert pc.tokens_out_for_sol(lo) <= pc.tokens_out_for_sol(hi)


@SETTINGS
@given(st.floats(min_value=0.01, max_value=40.0), st.floats(min_value=0.01, max_value=40.0))
def test_curve_concave_split_buys_never_beat_single(a, b):
    """Buying a then b on a moving curve can't beat one buy from zero
    for each piece separately: f(a)+f(b) >= f(a+b) (concavity)."""
    assert pc.tokens_out_for_sol(a) + pc.tokens_out_for_sol(b) >= \
        pc.tokens_out_for_sol(a + b) - 1


@SETTINGS
@given(st.floats(min_value=0.001, max_value=80.0))
def test_curve_matches_exact_rational(s):
    net = int(s * 1_000_000_000 * (1 - pc.TOTAL_FEE_BPS / 10_000))
    exact = Fraction(pc.VTR0 * net, pc.VSR0 + net)
    out = pc.tokens_out_for_sol(s)
    assert out == int(exact)  # floor, no float drift


@pytest.mark.parametrize("s", [0, -1, -1e-12, float("-inf")])
def test_curve_non_positive(s):
    assert pc.tokens_out_for_sol(s) == 0


def test_curve_fee_is_conservative_vs_onchain_fee_on_top():
    """pump.fun charges the fee ON TOP of the curve cost: net = S/(1+f).
    The code uses S*(1-f), which is smaller -> expected tokens is a
    lower bound of what the dev buy really gets (safe direction)."""
    for s in (0.1, 1, 10, 50, 80):
        f = pc.TOTAL_FEE_BPS / 10_000
        onchain_net = int(s * 1e9 / (1 + f))
        onchain = pc.VTR0 * onchain_net // (pc.VSR0 + onchain_net)
        assert pc.tokens_out_for_sol(s) <= onchain


@pytest.mark.parametrize("raised", [96.0, 120.0, 300.0])
def test_curve_never_exceeds_real_reserves(raised):
    dev_buy = raised * 0.9
    assert pc.tokens_out_for_sol(dev_buy) <= pc.RTR0


def test_verify_params_roundtrip():
    raw = bytearray(200)
    for off, v in ((73, pc.VTR0), (81, pc.VSR0), (89, pc.RTR0),
                   (97, pc.SUPPLY), (105, pc.PROTOCOL_FEE_BPS)):
        raw[off:off + 8] = v.to_bytes(8, "little")
    on = pc.verify_params(bytes(raw))
    assert pc.params_match(on)
    for k in on:
        bad = dict(on)
        bad[k] += 1
        assert not pc.params_match(bad)


def test_verify_params_short_account_does_not_match():
    assert not pc.params_match(pc.verify_params(b"\x00" * 50))


# ---------------- distribution ----------------

int_weights = st.dictionaries(st.integers(1, 10**12), st.integers(1, 10**15),
                              min_size=1, max_size=MAX_PARTICIPANTS)
float_weights = st.dictionaries(
    st.integers(1, 10**12),
    st.floats(min_value=0.01, max_value=500.0, allow_nan=False),
    min_size=1, max_size=MAX_PARTICIPANTS)
tokens = st.integers(1, pc.RTR0)
bps = st.integers(0, 2_000)


def _check(weights, tokens_base, team_bps, safety_bps):
    out, team, dust = compute_distribution(weights, tokens_base, team_bps, safety_bps)
    distributable = tokens_base * (10_000 - team_bps - safety_bps) // 10_000
    shares = [s for _, s in out]
    assert [u for u, _ in out] == sorted(weights)            # deterministic order
    assert all(s >= 0 for s in shares)
    assert dust >= 0
    assert sum(shares) + dust == distributable
    assert sum(shares) + team <= tokens_base                  # never over-distribute
    assert dust < len(weights) + 1                            # floor loses < 1/user
    total_w = Fraction(sum(Fraction(w) for w in weights.values()))
    for uid, s in out:
        exact = Fraction(distributable) * Fraction(weights[uid]) / total_w
        assert abs(Fraction(s) - exact) < 2                   # proportional
    return out


@SETTINGS
@given(int_weights, tokens, bps, bps)
def test_distribution_invariants_int(weights, tokens_base, team_bps, safety_bps):
    _check(weights, tokens_base, team_bps, safety_bps)


@SETTINGS
@given(float_weights, tokens, bps, bps)
def test_distribution_invariants_float_weights(weights, tokens_base, team_bps, safety_bps):
    """Real launches pass FLOAT weights (SQLite REAL)."""
    _check(weights, tokens_base, team_bps, safety_bps)


def test_distribution_amounts_are_ints_with_real_ledger_weights():
    out, _, _ = compute_distribution({1: 0.1, 2: 0.2, 3: 0.7}, pc.RTR0, 0, 10)
    assert all(type(s) is int for _, s in out)


@SETTINGS
@given(int_weights, tokens)
def test_distribution_permutation_invariant(weights, tokens_base):
    a = dict(compute_distribution(weights, tokens_base, 0, 10)[0])
    b = dict(compute_distribution(dict(reversed(list(weights.items()))),
                                  tokens_base, 0, 10)[0])
    assert a == b


@SETTINGS
@given(st.integers(1, 10**9), st.integers(2, MAX_PARTICIPANTS), tokens)
def test_equal_weights_equal_shares(w, n, tokens_base):
    out, _, _ = compute_distribution({i: w for i in range(n)}, tokens_base, 0, 0)
    assert len({s for _, s in out}) == 1


def test_distribution_degenerate():
    assert compute_distribution({}, 10**9, 0, 0) == ([], 0, 0)
    assert compute_distribution({1: 5}, 0, 0, 0) == ([], 0, 0)
    assert compute_distribution({1: 0}, 10**9, 0, 0) == ([], 0, 0)


def test_distribution_negative_weight_is_not_rejected():
    """A negative net contribution (refund > deposit) reaching the
    distribution would produce a negative share. run_launch filters c>0,
    but compute_distribution itself has no guard."""
    out, _, _ = compute_distribution({1: 10, 2: -1}, 10**9, 0, 0)
    assert any(s < 0 for _, s in out)


def test_distribution_rejects_overflowing_bps():
    with pytest.raises(ValueError):
        compute_distribution({1: 1}, 10**9, 9_000, 2_000)


# ---------------- chunking ----------------

@SETTINGS
@given(st.integers(0, MAX_PARTICIPANTS))
def test_chunks_partition(n):
    rs = list(range(n))
    chunks = plan_chunks(rs)
    assert [x for c in chunks for x in c] == rs
    assert all(1 <= len(c) <= CHUNK_SIZE for c in chunks)
    assert len(chunks) <= 4


@SETTINGS
@given(st.integers(MAX_PARTICIPANTS + 1, 5_000))
def test_chunks_reject_over_capacity(n):
    with pytest.raises(ValueError):
        plan_chunks(list(range(n)))
