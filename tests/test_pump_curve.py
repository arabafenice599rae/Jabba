import pytest

from curvebot.pump_curve import (DECIMALS, PROTOCOL_FEE_BPS, RTR0, SUPPLY,
                                 TOTAL_FEE_BPS, VSR0, VTR0, params_match,
                                 tokens_out_for_sol, ui_to_base,
                                 verify_params)


def test_zero():
    assert tokens_out_for_sol(0) == 0


def test_integer_output():
    out = tokens_out_for_sol(1.0)
    assert isinstance(out, int)
    assert out > 0


def test_monotonic():
    prev = 0
    for sol in (0.1, 0.5, 1, 5, 20, 85):
        assert tokens_out_for_sol(sol) > prev
        prev = tokens_out_for_sol(sol)


def test_graduation_amount():
    """~85 SOL buys ~793M tokens (the real reserves) -> graduation."""
    out = tokens_out_for_sol(85.0)
    assert 700_000_000 * 10**DECIMALS < out < 850_000_000 * 10**DECIMALS


def test_params_verified_onchain():
    """Values decoded from the live pump.fun global account (2026-11)."""
    onchain = {
        "initial_virtual_token_reserves": 1_073_000_000 * 10**6,
        "initial_virtual_sol_reserves": 30_000_000_000,
        "initial_real_token_reserves": 793_100_000 * 10**6,
        "token_total_supply": 1_000_000_000 * 10**6,
        "fee_basis_points": 95,
    }
    assert params_match(onchain)
    assert VTR0 == onchain["initial_virtual_token_reserves"]
    assert VSR0 == onchain["initial_virtual_sol_reserves"]
    assert RTR0 == onchain["initial_real_token_reserves"]
    assert SUPPLY == onchain["token_total_supply"]
    assert PROTOCOL_FEE_BPS == 95
    assert TOTAL_FEE_BPS == 125  # 95 protocol + 30 creator


def test_verify_params_layout():
    """verify_params decodes the global account layout correctly."""
    raw = bytearray(128)
    raw[:8] = bytes.fromhex("a7e8e8b1c86c727f")
    def put(off, val): raw[off:off+8] = val.to_bytes(8, "little")
    put(73, VTR0); put(81, VSR0); put(89, RTR0); put(97, SUPPLY); put(105, 95)
    decoded = verify_params(bytes(raw))
    assert params_match(decoded)


def test_ui_base():
    assert ui_to_base(123.456789) == 123_456_789
