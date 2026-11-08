"""Render the expected pump.fun bonding curve as a PNG chart."""

from __future__ import annotations

import io


def render_pump_curve_png(dev_buy_sol: float = 0.0) -> bytes:
    """pump.fun constant-product curve: price vs tokens sold, with the
    expected dev-buy point marked. Returns PNG bytes."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from .pump_curve import DECIMALS, VSR0, VTR0, tokens_out_for_sol

    # marginal price (SOL per token) after s tokens (ui) sold:
    # p = (VSR0/1e9) / (1 - s_ui/1e9)  -- derived from constant product
    def price(s_ui: float) -> float:
        remaining_base = VTR0 - s_ui * 10 ** DECIMALS
        k = (VSR0 / 1_000_000_000) * VTR0 / remaining_base
        return k / 10 ** DECIMALS

    # tokens_out_for_sol returns BASE units; the chart works in UI tokens
    dev_tokens = (tokens_out_for_sol(dev_buy_sol) / 10 ** DECIMALS
                  if dev_buy_sol > 0 else 0.0)
    x_max = max(dev_tokens * 1.6, 100_000_000.0)
    steps = 240
    xs = [x_max * i / steps for i in range(steps + 1)]
    ys = [price(x) for x in xs]

    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=150)
    fig.patch.set_facecolor("#0e1117")
    ax.set_facecolor("#0e1117")

    ax.plot(xs, ys, color="#14f195", linewidth=2.4, label="pump.fun curve")
    ax.fill_between(xs, ys, color="#14f195", alpha=0.12)

    if dev_tokens > 0:
        p_now = price(dev_tokens)
        ax.axvline(dev_tokens, color="#9945ff", linewidth=1.2,
                   linestyle="--", alpha=0.8)
        ax.plot([dev_tokens], [p_now], "o", color="#f4ce4a",
                markersize=9, zorder=5)
        ax.annotate(
            f"  dev buy\n  {dev_tokens:,.0f} tok\n  {p_now:.10f} SOL",
            xy=(dev_tokens, p_now),
            xytext=(min(dev_tokens + x_max * 0.05, x_max * 0.6),
                    ys[-1] * 0.35),
            color="#f4ce4a", fontsize=9,
        )
    else:
        ax.annotate("virgin curve\n(no dev buy yet)",
                    xy=(x_max * 0.55, ys[-1] * 0.35),
                    color="#8b949e", fontsize=10)

    ax.set_title("pump.fun bonding curve  ·  price (SOL) vs supply sold (token)",
                 color="#e6edf3", fontsize=12, pad=12)
    ax.set_xlabel("Tokens sold", color="#8b949e")
    ax.set_ylabel("Price (SOL)", color="#8b949e")
    ax.tick_params(colors="#8b949e")
    for spine in ax.spines.values():
        spine.set_color("#30363d")
    ax.grid(color="#21262d", linewidth=0.6)
    ax.legend(facecolor="#0e1117", labelcolor="#e6edf3",
              edgecolor="#30363d", fontsize=9)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()
