"""Linear bonding curve math.

Price at supply s (tokens, ui units):  p(s) = P0 + M * s
Cost to buy x tokens from supply s:   C = P0*x + M*(x^2)/2 + M*s*x
Proceeds selling x tokens at supply s: P = P0*x + M*(s*x - x^2)/2
Sell fee is applied on the raw proceeds.
"""

from dataclasses import dataclass

LAMPORTS = 1_000_000_000


@dataclass(frozen=True)
class LinearCurve:
    price_base_sol: float   # P0: SOL per token at supply 0
    slope_sol: float        # M: SOL per token^2

    def price_sol(self, supply_tokens: float) -> float:
        return self.price_base_sol + self.slope_sol * supply_tokens

    def tokens_out_for_sol(self, amount_sol: float, supply_tokens: float) -> float:
        """Tokens received for spending amount_sol, starting at supply_tokens."""
        if amount_sol <= 0:
            return 0.0
        m = self.slope_sol
        if m <= 0:
            return amount_sol / self.price_base_sol
        # P0*x + m*s*x + (m/2)x^2 = amount
        b = self.price_base_sol + m * supply_tokens
        disc = b * b + 2.0 * m * amount_sol
        return (-b + disc ** 0.5) / m

    def cost_sol(self, tokens: float, supply_tokens: float) -> float:
        if tokens <= 0:
            return 0.0
        return (
            self.price_base_sol * tokens
            + self.slope_sol * supply_tokens * tokens
            + self.slope_sol * tokens * tokens / 2.0
        )

    def sell_proceeds_sol(self, tokens: float, supply_tokens: float,
                           sell_fee_bps: int = 0) -> tuple[float, float]:
        """Gross proceeds and net (after fee) for selling tokens at current supply."""
        if tokens <= 0 or supply_tokens <= 0:
            return 0.0, 0.0
        sold = min(tokens, supply_tokens)
        gross = (
            self.price_base_sol * sold
            + self.slope_sol * (supply_tokens * sold - sold * sold / 2.0)
        )
        net = gross * (1.0 - sell_fee_bps / 10_000.0)
        return gross, net
