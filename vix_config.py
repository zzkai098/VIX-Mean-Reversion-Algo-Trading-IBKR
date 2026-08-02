# =============================================================================
# VIX Directional Futures Strategy — configuration
# =============================================================================
# Every value below is either (a) selected by the walk-forward grid search in
# vix_backtest.py, or (b) a live-trading control the backtest does not simulate.
# Each block says which, so a reader can tell an optimised parameter from an
# engineering judgement call.
#
# Grid-selected values come from 4,374 parameter sets scored across 216 rolling
# 2-month windows of VIX history (1990-2026), ranked by average Sharpe rather
# than by average return: the return-ranked winner earns ~30% more per window
# but has a far deeper worst-window drawdown (docs/img/objective_tradeoff.png).
# =============================================================================

# ========= Signal parameters — GRID-SELECTED (rank 1 by average Sharpe) ======
Z_LOOKBACK = 20          # Rolling window for the VIX-spot z-score (trading days).
                         # 20d beat 10d/15d on Sharpe: a slower baseline treats a
                         # volatility spike as a genuine outlier instead of quickly
                         # re-centring on it.
Z_ENTRY_SHORT = 0.3      # Short VX1 when z >= this (sigma). Deliberately lower than
                         # the long threshold: VIX is right-skewed, so upside
                         # dislocations mean-revert more reliably than downside ones.
Z_ENTRY_LONG = 1.0       # Long VX1 when z <= -this (sigma).
Z_EXIT = 0.3             # Final exit threshold (sigma). Exiting slightly before the
                         # mean (0.3 rather than 0.0) scored better — the last leg of
                         # the reversion is the slowest and least reliable.
Z_STOP = 2.0             # Stop-loss offset (sigma) from the entry z-score.
MAX_HOLD_DAYS = 3        # Maximum holding period. Short holds dominated the grid:
                         # the edge is a fast reversion, and carrying past ~3 days
                         # mostly adds variance.

# Graduated profit-taking: (z_remaining_ratio, cumulative_close_pct).
# z_remaining_ratio = current_z / entry_z; when z decays past a tier, close up to
# that cumulative fraction of the original position.
PROFIT_TAKE_TIERS = [
    (0.75, 0.25),   # z retraced 25% -> close 25%
    (0.50, 0.50),   # z retraced 50% -> close 50%
    (0.25, 0.75),   # z retraced 75% -> close 75%
    (0.00, 1.00),   # z back to the mean -> close the rest
]

# ========= Entry confirmation ================================================
SLOPE_LOOKBACK = 2           # Days used to measure the direction of VIX.
SLOPE_CONFIRMATION = False   # Grid-selected: requiring slope agreement suppressed
                             # too many entries. With this off SLOPE_LOOKBACK is
                             # inert (all three grid values scored identically).
Z_BASIS_OVERRIDE = 1.8       # z above this ignores backwardation and allows a short:
                             # at ~2 sigma the reversion signal is strong enough to
                             # act on even when the term structure is unhelpful.
                             # Rounded from a hand-tuned 1.82; the extra digit implied
                             # precision the data does not support. Not grid-searched.
Z_CONTANGO_OVERRIDE = -1.4   # z below this ignores contango and allows a long.
Z_STOP_OVERRIDE = 1.5        # Tighter stop for override entries, which trade against
                             # the term structure and so deserve less rope.
VOLUME_LOOKBACK = 20         # Rolling window for average volume.
VOLUME_MULTIPLIER = 1.0      # Entry requires volume >= multiplier x rolling average.
VOLUME_CONFIRMATION = True   # Skip entries on thin tape.

# ========= Regime filter — live only, not in the backtest ====================
VIX_REGIME_THRESHOLD = 35         # VIX >= 35 blocks new LONG entries (shorts stay
                                  # allowed: elevated VIX is where short-vol pays).
VIX_REGIME_REDUCE_THRESHOLD = 30  # VIX 30-35 halves new long exposure.
REGIME_REDUCE_FACTOR = 0.5

# ========= Risk limits — live controls, not simulated ========================
# The backtest sizes positions with no margin or liquidation model, so its
# worst-window drawdowns are unrealistically deep. These caps are the live
# safeguards that stop a losing window from compounding.
MAX_LOSS_PCT = 0.04          # Max unrealised loss per trade, as a fraction of account.
DAILY_MAX_LOSS_PCT = 0.05    # Cumulative daily loss cap; trading halts if breached.

# ========= Position sizing — deliberately below the backtest's ===============
CAPITAL_PER_CONTRACT = 1000  # VIX futures multiplier: $1,000 per index point.
MAX_CONTRACTS = 15           # Live cap. The grid search sized up to 20 contracts;
                             # 15 is a conscious haircut, because the backtest cannot
                             # model a margin call and its tail is correspondingly
                             # optimistic.
# Z-score tiered sizing: (|z| threshold, contracts). Larger dislocation, larger clip.
POSITION_SIZE_TIERS = [
    (2.5, 15),
    (2.0, 12),
    (1.5, 9),
    (1.0, 6),
    (0.75, 4),   # minimum entry size
]
CAPITAL_ALLOCATION_PCT = 0.5  # Share of the account allocated to this strategy.

# ========= Market data =======================================================
SPREAD_HISTORY_DAYS = 120  # Days of VIX history kept in memory for the z-score.
DATA_DURATION = '6 M'      # Historical request window sent to IBKR.
DATA_BAR_SIZE = '1 day'

# ========= Scheduling ========================================================
DAILY_SIGNAL_TIME = "15:30"   # Daily signal check, 30 min before the US close.
FRIDAY_CLOSE_TIME = "15:45"   # Optional Friday flat-out, 30 min before the close.
FRIDAY_CLOSE_ENABLED = False  # True to avoid carrying weekend gap risk.
RISK_INTERVAL_MINUTES = 1     # Intraday risk/regime monitoring cadence.
CHART_EVERY_N_TICKS = 1       # Terminal chart refresh, in risk-check ticks.

# ========= Execution =========================================================
REQUIRE_APPROVAL = True   # True  = print the order and wait for a y/n at the console.
                          # False = submit automatically (still gated by the z-score,
                          #         basis, cooldown and stop-loss checks).
                          # Shipped True so a fresh clone cannot trade unattended.
VERBOSE = True

# ========= IBKR connection ===================================================
# Paper-trading defaults. Point IB_ACCOUNT / IB_PORT at a live account only after
# reading the risk section of the README.
IB_ACCOUNT = "DUP323189"   # "DU" prefix = IBKR paper account.
IB_HOST = "127.0.0.1"
IB_PORT = 7497             # TWS paper: 7497 | Gateway paper: 4002
IB_CLIENT_ID = 20
BASE_CURRENCY = "USD"
EXCHANGE = "SMART"

# ========= News sentiment (GDELT + VADER) — live only ========================
# GDELT 2.0 only covers 2015 onward, so this filter cannot be evaluated over the
# 1990-2026 backtest window. It is a live overlay, not a backtested edge.
NEWS_SENTIMENT_ENABLED         = True
# Asymmetric by design: fear should gate a short faster than relief gates a long.
NEWS_FEAR_THRESHOLD_STRONG     = 0.20   # score < -this: block SHORT / favour LONG
NEWS_FEAR_THRESHOLD_MODERATE   = 0.10   # score < -this: label only
NEWS_RELIEF_THRESHOLD_STRONG   = 0.40   # score > +this: block LONG / reinforce SHORT
NEWS_RELIEF_THRESHOLD_MODERATE = 0.25   # score > +this: label only
NEWS_CACHE_MINUTES             = 30     # GDELT poll cadence.
NEWS_SENTIMENT_ENTRY_BOOST     = 0.10   # Relax Z_ENTRY_LONG when fear news agrees.
# Example keyword set from the period this was run. Replace these with whatever
# macro theme is actually driving volatility — the filter is only as good as its query.
NEWS_KEYWORDS = [
    "Iran war", "Iran attack", "Iran conflict", "Iran strike",
    "IRGC", "Strait of Hormuz", "Persian Gulf",
    "US-Iran", "Iran nuclear", "Iran sanctions", "Iran missile",
    "Iran military", "Iran retaliation", "Iran threat",
]

# ========= Regime-shift detection — live only ================================
# Compares a fast and a slow z-score; a large gap means the distribution itself is
# moving, so reverting toward a stale mean is the wrong trade.
REGIME_SHIFT_ENABLED       = True
Z_LOOKBACK_LONG            = 30     # Slow lookback, against Z_LOOKBACK as the fast one.
REGIME_SHIFT_DIVERGENCE    = 1.0    # |z_slow - z_fast| above this flags a regime shift.
REGIME_SHIFT_BLOCK_ENTRY   = True
REGIME_SHIFT_BLOCK_PYRAMID = True

# ========= Pyramiding — live only ============================================
PYRAMID_ENABLED        = True
PYRAMID_MAX_ADDS       = 3      # Adds allowed on top of the initial entry.
PYRAMID_MIN_Z_GAP      = 0.25   # Required further z-score extension between adds.
PYRAMID_STOP_DECAY     = 0.90   # Each add tightens the stop offset by this factor.
PYRAMID_COOLDOWN_HOURS = 0.5    # Minimum spacing between adds.
