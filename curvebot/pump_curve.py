"""pump.fun bonding curve math (constant product), parameters VERIFIED
against the live on-chain global account of the pump program
(4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf, program
6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P) on 2026-11:

    initial_virtual_token_reserves = 1_073_000_000 * 10**6
    initial_virtual_sol_reserves   = 30 SOL
    initial_real_token_reserves   = 793_100_000 * 10**6
    token_total_supply            = 1_000_000_000 * 10**6
    fee_basis_points (protocol)   = 95

The creator fee (30 bps) is charged on top of the protocol fee.

How the dev buy really executes (measured on a mainnet fork, 2026-11-08,
PumpPortal trade-local create, amount=1 SOL, denominatedInSol):
  - `amount` is the SOL that goes INTO THE CURVE; tokens received are exactly
    floor(VTR0 * amount / (VSR0 + amount))  (34_612_903_225_806 for 1 SOL)
  - fees are charged ON TOP: protocol 95 bps + creator 30 bps + PumpPortal 50 bps
  - plus one-off rent for the accounts the create opens (mint with metadata,
    bonding curve, its ATA, treasury ATA, creator vault, volume accumulator)
  - the mint is Token-2022 (create_v2)
Re-verify the parameters before launch with `verify_params()`.
"""

VTR0 = 1_073_000_000 * 10 ** 6    # initial virtual token reserves (base units)
VSR0 = 30 * 1_000_000_000         # initial virtual SOL reserves (lamports)
RTR0 = 793_100_000 * 10 ** 6      # initial real token reserves (base units)
SUPPLY = 1_000_000_000 * 10 ** 6  # total supply (base units)
PROTOCOL_FEE_BPS = 95             # on-chain global.fee_basis_points
CREATOR_FEE_BPS = 30              # creator share (separate fee program)
PUMPPORTAL_FEE_BPS = 50           # PumpPortal local API fee (measured)
TOTAL_FEE_BPS = PROTOCOL_FEE_BPS + CREATOR_FEE_BPS
# sum of rents opened by create+buy, measured with the classic rent
# (3480 x 2y). Mainnet rent is ~27% lower since 2026: an upper bound.
CREATE_RENT_LAMPORTS = 12_235_680
DECIMALS = 6
# SOL into the curve at which the dev buy would take all real reserves
MAX_DEV_BUY_LAMPORTS = VSR0 * RTR0 // (VTR0 - RTR0)


def sol_to_lamports(sol: float) -> int:
    return round(sol * 1_000_000_000)


def tokens_out_for_sol(sol_into_curve: float) -> int:
    """EXACT token output (base units) for `sol_into_curve` SOL entering a
    virgin curve (fees are paid on top, see module doc). Integer floor.
    Capped at the real token reserves."""
    if not sol_into_curve or sol_into_curve <= 0:
        return 0
    lamports = sol_to_lamports(sol_into_curve)
    return min(VTR0 * lamports // (VSR0 + lamports), RTR0)


def dev_buy_total_cost_lamports(sol_into_curve: float) -> int:
    """Everything the treasury pays for the create+dev-buy tx except the
    priority fee: curve SOL + all fees on top + one-off create rents."""
    lamports = sol_to_lamports(sol_into_curve)
    fees = lamports * (TOTAL_FEE_BPS + PUMPPORTAL_FEE_BPS) // 10_000
    return lamports + fees + CREATE_RENT_LAMPORTS


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
    return round(ui * 10 ** DECIMALS)
