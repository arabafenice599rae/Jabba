"""pump.fun bonding curve math (constant product), parameters VERIFIED
against the live on-chain global account of the pump program
(4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf, program
6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P) on 2026-11:

    initial_virtual_token_reserves = 1_073_000_000 * 10**6
    initial_virtual_sol_reserves   = 30 SOL
    initial_real_token_reserves   = 793_100_000 * 10**6
    token_total_supply            = 1_000_000_000 * 10**6
    fee_basis_points (protocol)   = 95

The creator fee (30 bps) is charged on top of the protocol fee, so the
total deduction for a buyer is 125 bps. Re-verify these before launch with
`verify_params()` against the global account.
"""

VTR0 = 1_073_000_000 * 10 ** 6    # initial virtual token reserves (base units)
VSR0 = 30 * 1_000_000_000         # initial virtual SOL reserves (lamports)
RTR0 = 793_100_000 * 10 ** 6      # initial real token reserves (base units)
SUPPLY = 1_000_000_000 * 10 ** 6  # total supply (base units)
PROTOCOL_FEE_BPS = 95             # on-chain global.fee_basis_points
CREATOR_FEE_BPS = 30              # creator share (separate fee program)
TOTAL_FEE_BPS = PROTOCOL_FEE_BPS + CREATOR_FEE_BPS
DECIMALS = 6


def tokens_out_for_sol(sol_gross: float) -> int:
    """EXACT token output (base units, integer) for a SOL spend on a
    virgin curve, with the total fee (protocol + creator) deducted.
    Integer floor — deterministic."""
    if sol_gross <= 0:
        return 0
    net_lamports = int(sol_gross * 1_000_000_000 * (1 - TOTAL_FEE_BPS / 10_000))
    out_base = VTR0 * net_lamports // (VSR0 + net_lamports)
    return out_base


def verify_params(raw_global_account: bytes) -> dict:
    """Decode a fetched global account and compare with the constants.
    Returns dict with the on-chain values; caller asserts equality."""
    def u64(off: int) -> int:
        return int.from_bytes(raw_global_account[off:off + 8], "little")
    return {
        "initial_virtual_token_reserves": u64(73),
        "initial_virtual_sol_reserves": u64(81),
        "initial_real_token_reserves": u64(89),
        "token_total_supply": u64(97),
        "fee_basis_points": u64(105),
    }


def params_match(onchain: dict) -> bool:
    return (
        onchain["initial_virtual_token_reserves"] == VTR0
        and onchain["initial_virtual_sol_reserves"] == VSR0
        and onchain["initial_real_token_reserves"] == RTR0
        and onchain["token_total_supply"] == SUPPLY
        and onchain["fee_basis_points"] == PROTOCOL_FEE_BPS
    )


def ui_to_base(ui: float) -> int:
    return int(round(ui * 10 ** DECIMALS))
