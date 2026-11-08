"""Pure math for the pump.fun bonding curve (constant product, v0 params).

A brand-new token's curve is deterministic: virtual reserves start fixed,
so given a SOL spend we can compute the exact token output offline.
This lets us pre-build the distribution transaction BEFORE the dev buy
lands — which is what makes the atomic Jito bundle possible.

pump.fun tokens use 6 decimals.
"""

VTR0 = 1_073_000_000 * 10 ** 6   # initial virtual token reserves (base units)
VSR0 = 30 * 1_000_000_000        # initial virtual SOL reserves (lamports)
FEE_BPS = 125                    # 1.25% trade fee
DECIMALS = 6


def tokens_out_for_sol(sol_gross: float) -> float:
    """Exact tokens (ui units) bought with sol_gross SOL on a virgin curve."""
    if sol_gross <= 0:
        return 0.0
    net_lamports = int(sol_gross * 1_000_000_000 * (1 - FEE_BPS / 10_000))
    out_base = VTR0 * net_lamports / (VSR0 + net_lamports)
    return out_base / 10 ** DECIMALS


def ui_to_base(ui: float) -> int:
    return int(round(ui * 10 ** DECIMALS))
