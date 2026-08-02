"""Regenerate the figures in docs/img/ from a grid-search result CSV.

    python make_figures.py grid_results.csv

Only metrics that survive the backtest's known defects are plotted. Trade win rate
and trade frequency are computed from realised trade P&L signs and counts, so they
are unaffected by the equity-path problem described in the README's Limitations
section. Ratio metrics (Sharpe, Calmar) and drawdown are deliberately NOT plotted:
the engine has no equity floor, roughly half the grid drives account equity below
zero, and once that happens those statistics are arithmetically meaningless.
"""
import sys
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

CSV = sys.argv[1] if len(sys.argv) > 1 else "grid_results.csv"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "img")
os.makedirs(OUT, exist_ok=True)

plt.rcParams.update({
    "figure.dpi": 140, "savefig.dpi": 140, "font.size": 9,
    "axes.grid": True, "grid.alpha": 0.25,
    "axes.spines.top": False, "axes.spines.right": False,
})
INK, COOL, WARM = "#1f2937", "#2563eb", "#dc2626"

df = pd.read_csv(CSV)
# slope_lookback is unreachable when slope_confirmation is False, so those rows are
# exact triplicates. De-duplicate before counting or plotting anything.
df.loc[~df.slope_confirmation, "slope_lookback"] = pd.NA
df = df.drop_duplicates(
    subset=["z_lookback", "z_entry_short", "z_entry_long", "z_exit", "z_stop",
            "slope_lookback", "max_hold_days", "slope_confirmation"])
print(f"{len(df):,} distinct parameter sets after de-duplication")

on, off = df[df.slope_confirmation], df[~df.slope_confirmation]

# --------------------------------------------------------------------------
# 1. One binary filter splits the whole search
# --------------------------------------------------------------------------
fig, (a1, a2) = plt.subplots(1, 2, figsize=(7.6, 3.3), sharey=True)
for ax, sub, title, colour in ((a1, off, "slope_confirmation = False", COOL),
                               (a2, on, "slope_confirmation = True", WARM)):
    ax.hist(sub.avg_win_rate, bins=30, color=colour, alpha=0.85)
    ax.set_title(f"{title}\n{len(sub):,} sets · median {sub.avg_win_rate.median():.1f}%",
                 loc="left", color=INK, fontsize=9)
    ax.set_xlabel("average trade win rate (%)")
a1.set_ylabel("parameter sets")
fig.suptitle("A single boolean, not the numeric parameters, splits the search",
             x=0.01, ha="left", color=INK, fontsize=10)
fig.tight_layout()
fig.savefig(os.path.join(OUT, "slope_filter_split.png"))
plt.close(fig)

# --------------------------------------------------------------------------
# 2. Win rate vs trade frequency — the actual trade-off being made
# --------------------------------------------------------------------------
fig, ax = plt.subplots(figsize=(6.2, 4.0))
ax.scatter(off.avg_trades_per_window, off.avg_win_rate, s=10, alpha=0.5,
           color=COOL, linewidths=0, label="slope_confirmation = False")
ax.scatter(on.avg_trades_per_window, on.avg_win_rate, s=10, alpha=0.5,
           color=WARM, linewidths=0, label="slope_confirmation = True")
ax.set_xlabel("average trades per window")
ax.set_ylabel("average trade win rate (%)")
ax.set_title("Selectivity costs frequency\nevery distinct parameter set in the grid",
             loc="left", color=INK)
ax.legend(frameon=False, loc="lower right")
fig.tight_layout()
fig.savefig(os.path.join(OUT, "winrate_vs_frequency.png"))
plt.close(fig)

# --------------------------------------------------------------------------
# 3. Sensitivity of win rate to the two structural parameters
# --------------------------------------------------------------------------
pivot = off.pivot_table(index="z_lookback", columns="max_hold_days",
                        values="avg_win_rate", aggfunc="mean")
fig, ax = plt.subplots(figsize=(5.0, 3.3))
im = ax.imshow(pivot.values, cmap="Blues", aspect="auto")
ax.set_xticks(range(len(pivot.columns)), [f"{c}d" for c in pivot.columns])
ax.set_yticks(range(len(pivot.index)), [f"{i}d" for i in pivot.index])
ax.set_xlabel("max holding period")
ax.set_ylabel("z-score lookback")
ax.set_title("Win rate is driven by lookback, barely by holding period\n"
             "slope_confirmation = False only", loc="left", color=INK, fontsize=9)
for i in range(pivot.shape[0]):
    for j in range(pivot.shape[1]):
        v = pivot.values[i, j]
        ax.text(j, i, f"{v:.1f}%", ha="center", va="center", fontsize=9,
                color="white" if v > pivot.values.mean() else INK)
ax.grid(False)
fig.colorbar(im, ax=ax, label="avg win rate (%)")
fig.tight_layout()
fig.savefig(os.path.join(OUT, "winrate_sensitivity.png"))
plt.close(fig)

print("figures written to", OUT)
for f in sorted(os.listdir(OUT)):
    print("  ", f)
print("\nwin rate by lookback / hold (slope_confirmation=False):")
print(pivot.round(2).to_string())
