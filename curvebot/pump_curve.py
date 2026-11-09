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

How the dev buy really executes (measured on a mainnet fork, 2026-11-09,
our own create_v2 + buy_v3, see pump_ix.py):
  - buy_v3 buys an EXACT token amount; for S SOL into a virgin curve that is
    floor(VTR0 * S / (VSR0 + S))  (34_612_903_225_806 for 1 SOL)
  - fees are charged ON TOP: protocol 95 bps + creator 30 bps (= 1.25%;
    with buy_v3 most of it stays on the curve until swept)
  - plus one-off rent for the accounts the create + buy open: the Token-2022
    mint (size grows with name/symbol/uri), bonding curve, its ATA, the
    treasury's ATA and its user_volume_accumulator
Re-verify the parameters before launch with `verify_params()`.
"""

VTR0 = 1_073_000_000 * 10 ** 6    # initial virtual token reserves (base units)
VSR0 = 30 * 1_000_000_000         # initial virtual SOL reserves (lamports)
RTR0 = 793_100_000 * 10 ** 6      # initial real token reserves (base units)
SUPPLY = 1_000_000_000 * 10 ** 6  # total supply (base units)
PROTOCOL_FEE_BPS = 95             # on-chain global.fee_basis_points
CREATOR_FEE_BPS = 30              # creator share (separate fee program)
TOTAL_FEE_BPS = PROTOCOL_FEE_BPS + CREATOR_FEE_BPS
# max fee we accept in buy_v3's max_sol_cost (and budget in the preflight):
# if pump.fun raises fees beyond this, the bundle fails atomically
FEE_CAP_BPS = TOTAL_FEE_BPS + 50
DECIMALS = 6
# SOL into the curve at which the dev buy would take all real reserves
MAX_DEV_BUY_LAMPORTS = VSR0 * RTR0 // (VTR0 - RTR0)

# sizes (bytes) of the accounts opened by create_v2 + ATA + buy_v3 (fork)
MINT_BASE_SIZE = 316              # + bytes of name + symbol + uri (metadata)
CURVE_SIZE, VOLUME_ACC_SIZE, TOKEN_ACCOUNT_SIZE = 166, 137, 170
CLASSIC_RENT_PER_BYTE = 3480 * 2  # pre-2026 rent: an upper bound if RPC fails


def create_account_sizes(name: str, symbol: str, uri: str) -> list[int]:
    """Accounts the launch's first tx creates and pays rent for:
    mint, bonding curve, curve ATA, treasury ATA, volume accumulator."""
    meta = len(name.encode()) + len(symbol.encode()) + len(uri.encode())
    return [MINT_BASE_SIZE + meta, CURVE_SIZE, TOKEN_ACCOUNT_SIZE,
            TOKEN_ACCOUNT_SIZE, VOLUME_ACC_SIZE]


def classic_rent(size: int) -> int:
    return (size + 128) * CLASSIC_RENT_PER_BYTE


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


def max_sol_cost_lamports(sol_into_curve: float) -> int:
    """buy_v3's max_sol_cost: curve SOL + fees at FEE_CAP_BPS, rounded up."""
    lamports = sol_to_lamports(sol_into_curve)
    return lamports + -(-lamports * FEE_CAP_BPS // 10_000) + 1


def curve_sol_affordable(budget_lamports: int) -> int:
    """Inverse of max_sol_cost_lamports: the most curve SOL (lamports) whose
    capped cost fits in `budget_lamports`."""
    if budget_lamports <= 1:
        return 0
    s = (budget_lamports - 1) * 10_000 // (10_000 + FEE_CAP_BPS)
    while s > 0 and s + -(-s * FEE_CAP_BPS // 10_000) + 1 > budget_lamports:
        s -= 1
    return s


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
