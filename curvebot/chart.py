"""Render the bonding curve as a PNG chart (price vs supply)."""

from __future__ import annotations

import io


def render_curve_png(price_base_sol: float, slope_sol: float,
                     supply_tokens: float, fee_pct: float = 0.30) -> bytes:
    """Draw the linear curve p(s) = P0 + M*s with the current supply marked.

    Returns PNG bytes. Pure CPU work, no network.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    x_max = max(supply_tokens * 1.6, 1000.0)
    steps = 240
    xs = [x_max * i / steps for i in range(steps + 1)]
    ys = [price_base_sol + slope_sol * x for x in xs]

    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=150)
    fig.patch.set_facecolor("#0e1117")
    ax.set_facecolor("#0e1117")

    ax.plot(xs, ys, color="#14f195", linewidth=2.4, label="Prezzo di acquisto")
    ax.fill_between(xs, ys, color="#14f195", alpha=0.12)

    if supply_tokens > 0:
        price_now = price_base_sol + slope_sol * supply_tokens
        ax.axvline(supply_tokens, color="#9945ff", linewidth=1.2, linestyle="--", alpha=0.8)
        ax.plot([supply_tokens], [price_now], "o", color="#f4ce4a", markersize=9, zorder=5)
        ax.annotate(
            f"  supply attuale\n  {supply_tokens:,.0f} tok\n  {price_now:.10f} SOL",
            xy=(supply_tokens, price_now),
            xytext=(min(supply_tokens + x_max * 0.06, x_max * 0.64), price_now * 0.5),
            color="#f4ce4a", fontsize=9,
        )
    else:
        ax.annotate("curva vergine\n(nessun token emesso)", xy=(x_max * 0.55, ys[-1] * 0.35),
                    color="#8b949e", fontsize=10)

    ax.set_title("Bonding curve  ·  prezzo (SOL) vs supply (token)",
                 color="#e6edf3", fontsize=12, pad=12)
    ax.set_xlabel("Supply (token)", color="#8b949e")
    ax.set_ylabel("Prezzo (SOL)", color="#8b949e")
    ax.tick_params(colors="#8b949e")
    for spine in ax.spines.values():
        spine.set_color("#30363d")
    ax.grid(color="#21262d", linewidth=0.6)
    ax.legend(facecolor="#0e1117", labelcolor="#e6edf3", edgecolor="#30363d", fontsize=9)
    ax.text(0.99, -0.16, f"fee buy/sell: {fee_pct:.2f}%",
            transform=ax.transAxes, ha="right", color="#8b949e", fontsize=8)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()
