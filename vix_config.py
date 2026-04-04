# ========== VIX Directional Futures Strategy Configuration ==========

# ========= Signal Parameters (Plan B: Offensive + Half Position) =========
Z_LOOKBACK = 10          # Rolling window for VIX spot z-score (days)
Z_ENTRY_SHORT = 0.75     # Entry threshold for shorting VX1 (σ)
Z_ENTRY_LONG = 1.0       # Entry threshold for longing VX1 (σ)
Z_EXIT = 0.0             # Final exit threshold (σ). Last tier closes all at Z=0
# Graduated profit-taking tiers: (z_remaining_ratio, cumulative_close_pct)
# z_remaining_ratio = current_z / entry_z; when Z drops below this ratio, close up to cumulative %
PROFIT_TAKE_TIERS = [
    (0.75, 0.25),   # Z retraced 25% → close 25% of original
    (0.50, 0.50),   # Z retraced 50% → close 50% of original
    (0.25, 0.75),   # Z retraced 75% → close 75% of original
    (0.00, 1.00),   # Z back to 0 → close 100%
]
Z_STOP = 2.0             # Stop loss relative offset (σ). Normal entries: entry_z + 2.0
MAX_HOLD_DAYS = 10       # Maximum holding period

# ========= Signal Confirmation =========
SLOPE_LOOKBACK = 2           # Days to measure VIX direction change
SLOPE_CONFIRMATION = False   # Disabled — more signals, no slope filter
Z_BASIS_OVERRIDE = 1.82        # Z > this → ignore backwardation, force allow short
Z_STOP_OVERRIDE = 1.5        # Tighter relative stop for basis-override entries (normal: Z_STOP=2.0)
VOLUME_LOOKBACK = 20         # Rolling window for average volume
VOLUME_MULTIPLIER = 0.5      # Entry requires volume >= multiplier × rolling avg
VOLUME_CONFIRMATION = True   # Require above-average volume for entry

# ========= Regime Filter (Tiered) =========
VIX_REGIME_THRESHOLD = 35         # VIX >= 35 → block new LONG entries (shorts OK — high VIX = good short)
VIX_REGIME_REDUCE_THRESHOLD = 30  # VIX 30-35 → reduce long position size
REGIME_REDUCE_FACTOR = 0.5        # Reduce to 50% of current contracts

# ========= Risk Limits =========
MAX_LOSS_PCT = 0.03          # Max unrealized loss per trade (% of account). 3% = $30k on $1M
DAILY_MAX_LOSS_PCT = 0.08    # Max cumulative daily loss (% of account). Stop trading if breached

# ========= Position Sizing =========
CAPITAL_PER_CONTRACT = 1000  # VIX futures multiplier: $1,000/point
MAX_CONTRACTS = 10           # Half position cap (was 20) — controls extreme drawdown
# Z-score based position sizing tiers: (z_threshold, contracts) — capped at 10
POSITION_SIZE_TIERS = [
    (2.5, 10),   # |z| >= 2.5 → 10 contracts (full size, capped)
    (2.0, 8),    # |z| >= 2.0 → 8 contracts
    (1.5, 6),    # |z| >= 1.5 → 6 contracts
    (1.0, 4),    # |z| >= 1.0 → 4 contracts
    (0.75, 3),   # |z| >= 0.75 → 3 contracts (minimum entry)
]
CAPITAL_ALLOCATION_PCT = 0.2  # Fraction of account allocated to this strategy

# ========= Data =========
SPREAD_HISTORY_DAYS = 120  # How many days of VIX history to maintain for z-score calc
DATA_DURATION = '6 M'      # Historical data request duration
DATA_BAR_SIZE = '1 day'    # Bar size for historical data

# ========= Schedule =========
DAILY_SIGNAL_TIME = "15:30"  # Run daily signal check at 3:30 PM EST (30 min before close)
FRIDAY_CLOSE_TIME = "15:45"  # Friday auto-close all positions (30 min before VIX futures close)
FRIDAY_CLOSE_ENABLED = False  # Set True to auto-close all positions on Friday before weekend
RISK_INTERVAL_MINUTES = 1    # Intraday regime monitoring frequency
CHART_EVERY_N_TICKS = 1      # Draw live terminal chart every N risk checks (1 = every minute)

# ========= Execution =========
REQUIRE_APPROVAL = True   # Auto-execute trades (protected by z-score + basis + cooldown + stop loss)
VERBOSE = True

# ========= IBKR Connection =========
IB_ACCOUNT = "DUP323189"
IB_HOST = "127.0.0.1"
IB_PORT = 7497             # TWS Paper: 7497 | Gateway Paper: 4002
IB_CLIENT_ID = 20
BASE_CURRENCY = "USD"
EXCHANGE = "SMART"

# ========= Geopolitical News Sentiment (GDELT + VADER) =========
NEWS_SENTIMENT_ENABLED            = True
# Asymmetric thresholds: fear triggers faster, relief triggers slower
NEWS_FEAR_THRESHOLD_STRONG        = 0.20   # effective_score < -this → block SHORT / boost LONG
NEWS_FEAR_THRESHOLD_MODERATE      = 0.10   # effective_score < -this → label only
NEWS_RELIEF_THRESHOLD_STRONG      = 0.40   # effective_score > +this → block LONG / reinforce SHORT
NEWS_RELIEF_THRESHOLD_MODERATE    = 0.25   # effective_score > +this → label only
NEWS_CACHE_MINUTES                = 15     # GDELT update cadence (minutes)
NEWS_SENTIMENT_ENTRY_BOOST        = 0.10   # Lower Z_ENTRY_LONG by this when strong negative news aligns
NEWS_KEYWORDS = [
    "Iran war", "Iran attack", "Iran conflict", "Iran strike",
    "IRGC", "Strait of Hormuz", "Persian Gulf",
    "US-Iran", "Iran nuclear", "Iran sanctions", "Iran missile",
    "Iran military", "Iran retaliation", "Iran threat",
]

# ========= Regime Shift Detection (Dual Lookback) =========
REGIME_SHIFT_ENABLED       = True
Z_LOOKBACK_LONG            = 30     # Long-term lookback for regime comparison
REGIME_SHIFT_DIVERGENCE    = 0.8    # |z_long - z_short| > this → regime shift warning
REGIME_SHIFT_BLOCK_ENTRY   = True   # Block new entries during detected regime shift
REGIME_SHIFT_BLOCK_PYRAMID = True   # Block pyramid adds during detected regime shift

# ========= Pyramiding (Add-to-Position) =========
PYRAMID_ENABLED        = True
PYRAMID_MAX_ADDS       = 2      # Maximum number of add-ons (initial entry + 2 adds)
PYRAMID_MIN_Z_GAP      = 0.5    # Min z-score increase between adds (same direction)
PYRAMID_STOP_DECAY     = 0.85   # Each add tightens stop offset by this factor
PYRAMID_COOLDOWN_HOURS = 24     # Min hours between adds
