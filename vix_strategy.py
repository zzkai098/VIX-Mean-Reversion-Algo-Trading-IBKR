"""
VIX Directional Futures Strategy — Live Implementation

Trades single-leg VX1 (front month VIX futures) based on VIX spot z-score.
  - VIX spot z > Z_ENTRY_SHORT → SHORT VX1 (VIX high, expect mean-reversion down)
  - VIX spot z < -Z_ENTRY_LONG  → LONG VX1 (VIX low, expect mean-reversion up)

Asymmetric thresholds: shorting easier (contango roll yield helps),
longing needs stronger signal (contango hurts long positions).

Six-layer exit: signal reversion, z-stop, absolute $ stop, max hold, daily max loss, tiered regime.

Classes:
    - VIXContractManager:  Resolves front month VIX futures contract on IBKR
    - VIXDataEngine:       Fetches VIX spot + VX1 prices, builds history for z-score
    - VIXSignalEngine:     Computes rolling z-score on VIX spot, generates directional signals
    - VIXRiskManager:      Six-layer exit logic + asymmetric regime filter
    - PositionTracker:     Manages open position state with persistence
    - VIXStrategyEngine:   Orchestrates the full daily workflow
    - VIXAutomationBot:    Schedules daily signal checks and intraday risk monitoring
"""
import pytz
import json
import math
import time
import schedule
import pandas as pd
import numpy as np
import yfinance as yf
from ib_async import *
from datetime import datetime
from pathlib import Path
from vix_config import *
from vix_ibkr import IBKR
from vix_news import VIXNewsEngine
import plotext as plt


# =============================================================================
# Live Terminal Chart
# =============================================================================
class LiveTerminalChart:
    """Collects intraday ticks and draws ASCII charts in terminal."""

    def __init__(self, max_ticks: int = 60):
        self.max_ticks = max_ticks
        self.ticks_time = []    # datetime labels
        self.ticks_vix = []
        self.ticks_vx1 = []
        self.ticks_z = []
        self.ticks_basis = []

    def add_tick(self, vix_spot: float, vx1_price: float, z_score: float):
        self.ticks_time.append(datetime.now().strftime('%H:%M'))
        self.ticks_vix.append(vix_spot)
        self.ticks_vx1.append(vx1_price)
        self.ticks_z.append(z_score)
        self.ticks_basis.append(vx1_price - vix_spot)
        # Trim to max
        if len(self.ticks_time) > self.max_ticks:
            self.ticks_time = self.ticks_time[-self.max_ticks:]
            self.ticks_vix = self.ticks_vix[-self.max_ticks:]
            self.ticks_vx1 = self.ticks_vx1[-self.max_ticks:]
            self.ticks_z = self.ticks_z[-self.max_ticks:]
            self.ticks_basis = self.ticks_basis[-self.max_ticks:]

    def draw(self, position=None):
        if len(self.ticks_time) < 2:
            return

        # Goldman Sachs style: navy background, gold accents, clean minimal
        GS_NAVY = (10, 15, 30)
        GS_BLUE = (100, 150, 255)       # Goldman blue — VIX Spot
        GS_GOLD = (198, 160, 60)
        GS_LIGHT = (180, 190, 205)
        GS_RED = (200, 60, 60)
        GS_GREEN = (60, 180, 120)
        GS_LAVENDER = (180, 140, 210)    # Z-Score neutral — lavender
        GS_GRID = (40, 50, 70)

        plt.clear_figure()
        plt.clear_color()
        plt.canvas_color(GS_NAVY)
        plt.axes_color(GS_NAVY)
        plt.ticks_color(GS_LIGHT)
        plt.subplots(2, 1)
        x = list(range(len(self.ticks_time)))
        labels = [self.ticks_time[i] if i % 5 == 0 else '' for i in x]

        # --- Top: VIX Spot + VX1 ---
        plt.subplot(1, 1)
        plt.canvas_color(GS_NAVY)
        plt.axes_color(GS_NAVY)
        plt.ticks_color(GS_LIGHT)
        plt.plot(x, self.ticks_vix, label="VIX Spot", color=GS_BLUE)
        plt.plot(x, self.ticks_vx1, label="VX1", color=GS_LIGHT)
        plt.title("VIX Spot vs VX1 (Intraday)")
        plt.ylabel("Price")
        plt.xticks(x, labels)

        if position:
            entry_price = position.get('entry_price', 0)
            plt.hline(entry_price, color=GS_GREEN)

        # --- Bottom: Z-Score ---
        plt.subplot(2, 1)
        plt.canvas_color(GS_NAVY)
        plt.axes_color(GS_NAVY)
        plt.ticks_color(GS_LIGHT)
        colors = []
        for z in self.ticks_z:
            if z > Z_ENTRY_SHORT:
                colors.append(GS_RED)
            elif z < -Z_ENTRY_LONG:
                colors.append(GS_GREEN)
            else:
                colors.append(GS_LAVENDER)
        plt.bar(x, self.ticks_z, label="Z-Score", color=colors)
        plt.hline(Z_ENTRY_SHORT, color=GS_RED)
        plt.hline(-Z_ENTRY_LONG, color=GS_GREEN)
        plt.hline(0, color=GS_GRID)
        plt.title("Z-Score (10d)")
        plt.ylabel("σ")
        plt.xticks(x, labels)

        plt.show()


# =============================================================================
# Contract Manager — resolves VIX futures front month
# =============================================================================
class VIXContractManager:
    """
    Manages VIX futures contract resolution on IBKR.
    For directional strategy, we only need VX1 (front month).
    """

    def __init__(self, ibkr: IBKR):
        self.ibkr = ibkr

    def get_vix_futures_chain(self) -> list:
        """Fetch all available VIX futures contracts from IBKR, sorted by expiry."""
        vix_fut = Future(symbol='VIX', exchange='CFE', currency='USD')
        details = self.ibkr.ib.reqContractDetails(vix_fut)

        if not details:
            print("[ERROR] No VIX futures contracts found. Check market data subscriptions.")
            return []

        contracts = [d.contract for d in details]
        contracts.sort(key=lambda c: c.lastTradeDateOrContractMonth)
        return contracts

    def _get_active_contracts(self, min_count: int = 1) -> list:
        """Filter and return active standard monthly VIX futures, sorted by expiry."""
        chain = self.get_vix_futures_chain()
        today = datetime.now().strftime('%Y%m%d')
        active = [c for c in chain
                  if c.lastTradeDateOrContractMonth >= today
                  and c.tradingClass == 'VX']
        if len(active) < min_count:
            print(f"[ERROR] Need {min_count} active VIX futures, found {len(active)}")
        return active

    def get_front_month(self) -> Future:
        """Returns VX1 — the nearest active standard monthly VIX futures contract."""
        active = self._get_active_contracts(1)
        if not active:
            return None
        vx1 = active[0]
        self.ibkr.ib.qualifyContracts(vx1)
        print(f"[CONTRACT] VX1: {vx1.localSymbol} (exp: {vx1.lastTradeDateOrContractMonth})")
        return vx1

    def get_front_and_second_month(self) -> tuple:
        """Returns (VX1, VX2) for compatibility."""
        active = self._get_active_contracts(2)
        if len(active) < 2:
            return None, None
        vx1, vx2 = active[0], active[1]
        self.ibkr.ib.qualifyContracts(vx1)
        self.ibkr.ib.qualifyContracts(vx2)
        return vx1, vx2

    def get_vix_index(self) -> Index:
        """Returns the CBOE VIX spot index contract for signal generation."""
        vix = Index('VIX', 'CBOE', 'USD')
        self.ibkr.ib.qualifyContracts(vix)
        return vix

    def days_to_expiry(self, contract) -> int:
        """Calculate days until contract expiry."""
        exp = datetime.strptime(contract.lastTradeDateOrContractMonth, '%Y%m%d')
        return (exp - datetime.now()).days


# =============================================================================
# Data Engine — fetches VIX spot + VX1 prices, builds history
# =============================================================================
class VIXDataEngine:
    """
    Fetches VIX spot (for signal), VX1 price (for execution), and maintains
    a rolling history for z-score computation.
    """

    def __init__(self, ibkr: IBKR, contract_mgr: VIXContractManager):
        self.ibkr = ibkr
        self.contract_mgr = contract_mgr
        self.vix_history = pd.DataFrame()  # columns: vix_spot, vx1, volume
        self._state_file = Path("vix_spot_history.csv")

    def load_state(self):
        """Load persisted VIX history from disk."""
        if self._state_file.exists() and self._state_file.stat().st_size > 0:
            try:
                self.vix_history = pd.read_csv(self._state_file, parse_dates=['date'], index_col='date')
                print(f"[DATA] Loaded {len(self.vix_history)} days of VIX history from disk.")
            except pd.errors.EmptyDataError:
                print("[DATA] History file is empty. Will build from scratch.")
        else:
            print("[DATA] No prior VIX history found. Will build from scratch.")

    def save_state(self):
        """Persist VIX history to disk."""
        if not self.vix_history.empty:
            self.vix_history.to_csv(self._state_file)
            print(f"[DATA] Saved {len(self.vix_history)} days of VIX history.")

    def fetch_historical_bars(self, contract, duration: str = DATA_DURATION,
                               bar_size: str = DATA_BAR_SIZE) -> pd.DataFrame:
        """Fetch historical OHLCV bars."""
        return self.ibkr.get_historical_prices(contract, duration=duration, bar_size=bar_size)

    def fetch_current_price(self, contract) -> float:
        """Fetch real-time (or delayed) price."""
        return self.ibkr.get_current_price(contract)

    def build_vix_history(self, vx1_contract) -> pd.DataFrame:
        """
        Build VIX spot + VX1 price history.
        VIX spot from yfinance (^VIX, accurate), VX1 from IBKR.
        """
        # Try yfinance first for real VIX spot history
        print("[DATA] Fetching VIX spot history from yfinance...")
        try:
            vix_yf = yf.download("^VIX", period="6mo", progress=False)
            if not vix_yf.empty:
                # Flatten multi-level columns if present
                if hasattr(vix_yf.columns, 'levels'):
                    vix_yf.columns = vix_yf.columns.get_level_values(0)
                vix_spot_series = vix_yf['Close'].dropna()
                vix_spot_series.index = pd.to_datetime(vix_spot_series.index).tz_localize(None)
                print(f"[DATA] Got {len(vix_spot_series)} days of VIX spot from yfinance.")
            else:
                vix_spot_series = pd.Series(dtype=float)
        except Exception as e:
            print(f"[WARN] yfinance VIX history failed: {e}")
            vix_spot_series = pd.Series(dtype=float)

        # Fetch VX1 history from IBKR
        print("[DATA] Fetching VX1 historical data from IBKR...")
        df_vx1 = self.fetch_historical_bars(vx1_contract)

        if df_vx1.empty and vix_spot_series.empty:
            print("[ERROR] Cannot build history — no data from any source.")
            return pd.DataFrame()

        # Build combined history
        if not vix_spot_series.empty and not df_vx1.empty:
            # Align on common dates
            common_idx = vix_spot_series.index.intersection(df_vx1.index)
            hist = pd.DataFrame({
                'vix_spot': vix_spot_series.reindex(common_idx),
                'vx1': df_vx1['close'].reindex(common_idx),
                'volume': df_vx1['volume'].reindex(common_idx, fill_value=0) if 'volume' in df_vx1.columns else 0
            }).dropna()
        elif not vix_spot_series.empty:
            # yfinance only — use VIX spot as VX1 proxy (better than nothing)
            hist = pd.DataFrame({
                'vix_spot': vix_spot_series,
                'vx1': vix_spot_series,
                'volume': 0
            }).dropna()
        else:
            # IBKR only — fallback to VX1 as VIX proxy (original behavior)
            hist = pd.DataFrame({
                'vix_spot': df_vx1['close'],
                'vx1': df_vx1['close'],
                'volume': df_vx1['volume'] if 'volume' in df_vx1.columns else 0
            }).dropna()

        hist.index.name = 'date'
        print(f"[DATA] Built VIX history: {len(hist)} days, "
              f"VIX spot range [{hist['vix_spot'].min():.2f}, {hist['vix_spot'].max():.2f}]")
        return hist

    def _fetch_vix_yfinance(self) -> float:
        """Fetch near-real-time VIX spot from Yahoo Finance."""
        try:
            vix = yf.Ticker("^VIX")
            price = vix.fast_info.get('lastPrice', float('nan'))
            if price and not math.isnan(price):
                return price
        except Exception as e:
            print(f"[WARN] yfinance VIX fetch failed: {e}")
        return float('nan')

    def fetch_vix_spot(self) -> float:
        """Fetch current VIX spot for signal generation and regime filtering."""
        price = self._fetch_vix_yfinance()
        if not math.isnan(price):
            return price

        print("[DATA] yfinance unavailable, falling back to IBKR.")
        vix_index = self.contract_mgr.get_vix_index()
        try:
            return self.ibkr.get_current_price(vix_index)
        except Exception:
            print("[ERROR] Could not get VIX spot from any source.")
            return float('nan')

    def fetch_latest_volume(self, contract) -> float:
        """Fetch latest daily bar volume."""
        try:
            df = self.ibkr.get_historical_prices(contract, duration='2 D', bar_size='1 day')
            if not df.empty and 'volume' in df.columns:
                return df['volume'].iloc[-1]
        except Exception as e:
            print(f"[WARN] Volume fetch failed: {e}")
        return 0.0

    def update_today(self, vix_spot: float, vx1_price: float, volume: float = 0.0):
        """Append today's data point to VIX history."""
        today = pd.Timestamp(datetime.now().date())

        new_row = pd.DataFrame({
            'vix_spot': [vix_spot],
            'vx1': [vx1_price],
            'volume': [volume],
        }, index=pd.DatetimeIndex([today], name='date'))

        if not self.vix_history.empty and today in self.vix_history.index:
            self.vix_history.loc[today] = new_row.iloc[0]
        else:
            self.vix_history = pd.concat([self.vix_history, new_row])

        max_rows = max(SPREAD_HISTORY_DAYS, Z_LOOKBACK * 3)
        if len(self.vix_history) > max_rows:
            self.vix_history = self.vix_history.iloc[-max_rows:]

        self.save_state()


# =============================================================================
# Signal Engine — z-score on VIX spot, directional signals
# =============================================================================
class VIXSignalEngine:
    """
    Computes rolling z-score of VIX spot price.
    Generates asymmetric entry signals:
      - SHORT when z > Z_ENTRY_SHORT (VIX high, contango helps)
      - LONG when z < -Z_ENTRY_LONG (VIX low, needs stronger signal)
    """

    def __init__(self, lookback: int = Z_LOOKBACK,
                 entry_short: float = Z_ENTRY_SHORT,
                 entry_long: float = Z_ENTRY_LONG):
        self.lookback = lookback
        self.entry_short = entry_short
        self.entry_long = entry_long

    def compute_zscore(self, series: pd.Series, lookback: int = None) -> pd.Series:
        """Rolling z-score: z = (value - rolling_mean) / rolling_std"""
        lb = lookback or self.lookback
        rolling_mean = series.rolling(window=lb).mean()
        rolling_std = series.rolling(window=lb).std().replace(0, np.nan)
        return (series - rolling_mean) / rolling_std

    def detect_regime_shift(self, vix_series: pd.Series, verbose: bool = False) -> dict:
        """
        Compare short-term z (10d) vs long-term z (30d).
        If they diverge significantly, the rolling mean is drifting = regime shift.

        Returns:
            {'detected': bool, 'z_short': float, 'z_long': float,
             'divergence': float, 'direction': 'up'|'down'|None}
        """
        if not REGIME_SHIFT_ENABLED or len(vix_series) < Z_LOOKBACK_LONG + 1:
            return {'detected': False, 'z_short': 0.0, 'z_long': 0.0,
                    'divergence': 0.0, 'direction': None}

        z_short = self.compute_zscore(vix_series, self.lookback).iloc[-1]
        z_long = self.compute_zscore(vix_series, Z_LOOKBACK_LONG).iloc[-1]
        divergence = z_long - z_short

        # Regime shift up: long-term z rising (VIX elevated vs 30d) but short-term z falling
        # (10d mean has caught up). divergence > threshold = mean drifting upward.
        detected = abs(divergence) > REGIME_SHIFT_DIVERGENCE
        direction = None
        if detected:
            direction = 'up' if divergence > 0 else 'down'

        if verbose and detected:
            print(f"[REGIME] ⚠ Shift detected ({direction}): "
                  f"z_short={z_short:.3f} vs z_long={z_long:.3f} "
                  f"(divergence={divergence:+.3f}, threshold=±{REGIME_SHIFT_DIVERGENCE})")

        return {'detected': detected, 'z_short': z_short, 'z_long': z_long,
                'divergence': divergence, 'direction': direction}

    def _check_slope_confirmation(self, vix_series: pd.Series, signal: str, verbose: bool) -> bool:
        """
        Check if VIX direction confirms mean-reversion.
        SHORT: VIX should be turning down (slope < 0)
        LONG:  VIX should be turning up (slope > 0)
        """
        if not SLOPE_CONFIRMATION:
            return True
        if len(vix_series) < SLOPE_LOOKBACK + 1:
            return True

        recent_slope = vix_series.iloc[-1] - vix_series.iloc[-1 - SLOPE_LOOKBACK]

        if verbose:
            print(f"Slope ({SLOPE_LOOKBACK}d): {recent_slope:+.4f}", end=" → ")

        if signal == 'LONG' and recent_slope < 0:
            if verbose:
                print("BLOCKED (VIX still falling)")
            return False
        elif signal == 'SHORT' and recent_slope > 0:
            if verbose:
                print("BLOCKED (VIX still rising)")
            return False

        if verbose:
            print("CONFIRMED")
        return True

    def _check_volume_confirmation(self, history: pd.DataFrame, verbose: bool) -> bool:
        """Check if current volume is above rolling average."""
        if not VOLUME_CONFIRMATION:
            return True
        if 'volume' not in history.columns:
            return True

        vol = history['volume']
        if len(vol) < VOLUME_LOOKBACK + 1:
            return True

        avg_vol = vol.rolling(VOLUME_LOOKBACK).mean().iloc[-1]
        current_vol = vol.iloc[-1]

        if avg_vol <= 0:
            return True

        ratio = current_vol / avg_vol

        if verbose:
            print(f"Volume: {current_vol:,.0f} vs avg {avg_vol:,.0f} "
                  f"(ratio: {ratio:.2f}, threshold: {VOLUME_MULTIPLIER})", end=" → ")

        if ratio < VOLUME_MULTIPLIER:
            if verbose:
                print("BLOCKED (below-average volume)")
            return False

        if verbose:
            print("CONFIRMED")
        return True

    def generate_signal(self, vix_history: pd.DataFrame, vix_spot: float,
                        vx1_price: float = None,
                        news_engine: 'VIXNewsEngine' = None,
                        verbose: bool = VERBOSE) -> dict:
        """
        Generate directional trading signal based on VIX spot z-score + basis filter.

        Returns dict with:
            signal: 'SHORT', 'LONG', or 'NO_SIGNAL'
            z_score: current z-score value
            vix_spot: current VIX spot level
            regime_ok: whether regime allows this trade direction
        """
        if len(vix_history) < self.lookback + 1:
            if verbose:
                print(f"[SIGNAL] Insufficient data ({len(vix_history)} < {self.lookback + 1}).")
            return {'signal': 'NO_SIGNAL', 'z_score': 0.0, 'vix_spot': vix_spot,
                    'regime_ok': True, 'reason': 'insufficient_data'}

        vix_series = vix_history['vix_spot']
        z_series = self.compute_zscore(vix_series)
        current_z = z_series.iloc[-1]

        # Regime shift detection (dual lookback)
        regime_shift = self.detect_regime_shift(vix_series, verbose=verbose)

        # Basis = VX1 - VIX Spot (positive = contango, negative = backwardation)
        basis = (vx1_price - vix_spot) if vx1_price is not None else None

        if verbose:
            print(f"\n--- VIX Signal Engine ---")
            print(f"VIX Spot: {vix_spot:.2f}")
            if vx1_price is not None:
                print(f"VX1 Price: {vx1_price:.2f} | Basis: {basis:+.2f} "
                      f"({'contango' if basis > 0 else 'backwardation'})")
            print(f"Z-Score: {current_z:.3f} (short: >{self.entry_short}, long: <-{self.entry_long})")
            if regime_shift['detected']:
                print(f"Z-Long (30d): {regime_shift['z_long']:.3f} | "
                      f"Divergence: {regime_shift['divergence']:+.3f}")

        # Block new entries during regime shift
        if regime_shift['detected'] and REGIME_SHIFT_BLOCK_ENTRY:
            if verbose:
                print(f"[REGIME] BLOCKED — regime shift {regime_shift['direction']} "
                      f"(z_short={regime_shift['z_short']:.3f} vs z_long={regime_shift['z_long']:.3f})")
            return {'signal': 'NO_SIGNAL', 'z_score': current_z, 'vix_spot': vix_spot,
                    'regime_ok': False, 'basis_override': False,
                    'reason': f'regime_shift_{regime_shift["direction"]} '
                              f'(div={regime_shift["divergence"]:+.3f})',
                    'sentiment_score': 0.0, 'sentiment_action': 'neutral',
                    'regime_shift': regime_shift}

        # Asymmetric z-score entry signals
        signal = None
        signal_at_boosted = None  # candidate LONG if z is within sentiment-boost window
        if current_z > self.entry_short:
            signal = 'SHORT'
        elif current_z < -self.entry_long:
            signal = 'LONG'
        elif current_z < -(self.entry_long - NEWS_SENTIMENT_ENTRY_BOOST):
            signal_at_boosted = 'LONG'  # promoted to real signal only by strong negative news

        # Basis filter — short requires contango, long requires backwardation
        if signal is not None and basis is not None:
            if signal == 'SHORT' and basis <= 0:
                if current_z > Z_BASIS_OVERRIDE:
                    if verbose:
                        print(f"Basis OVERRIDE: Z={current_z:.2f} > {Z_BASIS_OVERRIDE}, ignoring backwardation (basis={basis:+.2f})")
                else:
                    if verbose:
                        print(f"Basis BLOCKED: VX1 in backwardation (basis={basis:+.2f}), too risky to short")
                    return {'signal': 'NO_SIGNAL', 'z_score': current_z, 'vix_spot': vix_spot,
                            'regime_ok': True, 'reason': f'basis_backwardation ({basis:+.2f})'}
            elif signal == 'LONG' and basis >= 0:
                if verbose:
                    print(f"Basis BLOCKED: VX1 in contango (basis={basis:+.2f}), contango hurts longs")
                return {'signal': 'NO_SIGNAL', 'z_score': current_z, 'vix_spot': vix_spot,
                        'regime_ok': True, 'reason': f'basis_contango ({basis:+.2f})'}

        # Regime filter — only blocks LONG entries (shorting high VIX is fine)
        if signal == 'LONG' and vix_spot >= VIX_REGIME_THRESHOLD:
            if verbose:
                print(f"Regime BLOCKED: VIX={vix_spot:.1f} >= {VIX_REGIME_THRESHOLD} (no longs)")
            return {'signal': 'NO_SIGNAL', 'z_score': current_z, 'vix_spot': vix_spot,
                    'regime_ok': False, 'reason': f'VIX={vix_spot:.1f} blocks longs'}

        # Confirmation filters
        if signal is not None:
            if not self._check_slope_confirmation(vix_series, signal, verbose):
                return {'signal': 'NO_SIGNAL', 'z_score': current_z, 'vix_spot': vix_spot,
                        'regime_ok': True, 'reason': 'slope_not_confirmed'}

            if not self._check_volume_confirmation(vix_history, verbose):
                return {'signal': 'NO_SIGNAL', 'z_score': current_z, 'vix_spot': vix_spot,
                        'regime_ok': True, 'reason': 'volume_not_confirmed'}

        # --- Sentiment filter (GDELT + VADER) ---
        sentiment_score = 0.0
        sentiment_action = 'neutral'

        if NEWS_SENTIMENT_ENABLED and news_engine is not None:
            sent = news_engine.get_sentiment()
            effective_score = sent['score'] * sent['confidence']

            if verbose:
                cached_str = "(cached)" if sent.get('cached') else "(fresh)"
                err_str = f" ERR:{sent['error']}" if sent.get('error') else ""
                print(f"[NEWS] score={sent['score']:+.3f} | conf={sent['confidence']:.2f} | "
                      f"n={sent['article_count']} {cached_str}{err_str}")

            # Asymmetric thresholds: fear side more sensitive, relief side more conservative
            if effective_score < -NEWS_FEAR_THRESHOLD_STRONG:
                # Strong negative: war escalation → VIX likely to spike
                if signal == 'SHORT':
                    if verbose:
                        print(f"[NEWS] BLOCKED SHORT — negative news {effective_score:+.3f} (VIX spike risk)")
                    return {'signal': 'NO_SIGNAL', 'z_score': current_z, 'vix_spot': vix_spot,
                            'regime_ok': True, 'basis_override': False,
                            'reason': f'news_blocked_short (score={sent["score"]:+.3f})',
                            'sentiment_score': sent['score'], 'sentiment_action': 'blocked_short'}
                elif signal == 'LONG':
                    sentiment_action = 'long_boosted'
                elif signal_at_boosted == 'LONG':
                    # Promote boosted candidate to real LONG signal
                    signal = 'LONG'
                    sentiment_action = 'long_boosted_new_entry'
                    if verbose:
                        print(f"[NEWS] LONG PROMOTED via negative news: "
                              f"Z={current_z:.3f} (boosted threshold "
                              f"{self.entry_long}→{self.entry_long - NEWS_SENTIMENT_ENTRY_BOOST:.2f})")

            elif effective_score > NEWS_RELIEF_THRESHOLD_STRONG:
                # Strong positive: de-escalation → VIX likely to fall
                if signal == 'LONG':
                    if verbose:
                        print(f"[NEWS] BLOCKED LONG — positive news {effective_score:+.3f} (VIX drop expected)")
                    return {'signal': 'NO_SIGNAL', 'z_score': current_z, 'vix_spot': vix_spot,
                            'regime_ok': True, 'basis_override': False,
                            'reason': f'news_blocked_long (score={sent["score"]:+.3f})',
                            'sentiment_score': sent['score'], 'sentiment_action': 'blocked_long'}
                elif signal == 'SHORT':
                    sentiment_action = 'short_reinforced'

            elif effective_score < -NEWS_FEAR_THRESHOLD_MODERATE:
                # Moderate negative: partial LONG boost (label only)
                if signal == 'LONG':
                    sentiment_action = 'long_moderate_boost'

            elif effective_score > NEWS_RELIEF_THRESHOLD_MODERATE:
                # Moderate positive: reinforce SHORT
                if signal == 'SHORT':
                    sentiment_action = 'short_moderate_reinforced'

            sentiment_score = sent['score']

        # Track if entry was via basis override (for tighter stop loss)
        basis_override = (signal == 'SHORT' and basis is not None and basis <= 0
                          and current_z > Z_BASIS_OVERRIDE)

        final_signal = signal or 'NO_SIGNAL'
        if verbose:
            sentiment_str = (f" | news={sentiment_score:+.3f} [{sentiment_action}]"
                             if NEWS_SENTIMENT_ENABLED and news_engine is not None else "")
            print(f"Signal: {final_signal}{sentiment_str}")
            print(f"------------------------\n")

        return {'signal': final_signal, 'z_score': current_z, 'vix_spot': vix_spot,
                'regime_ok': True, 'basis_override': basis_override,
                'reason': ('z_score_trigger' if signal else 'within_band'),
                'sentiment_score': sentiment_score,
                'sentiment_action': sentiment_action}


# =============================================================================
# Risk Manager — six-layer exit system (directional)
# =============================================================================
class VIXRiskManager:
    """
    Six-layer exit for directional VX1 positions:
    1. Signal exit:      z-score reverts inside exit band → take profit
    2. Stop loss:        z-score exceeds stop threshold → cut losses
    3. Absolute $ stop:  unrealized loss exceeds % of account → hard stop
    4. Max hold:         position held longer than max days → forced exit
    5. Daily max loss:   cumulative daily losses exceed % of account → stop trading
    6. Regime (tiered):  only affects LONG positions (shorts thrive in high VIX)
    """

    def __init__(self, exit_z: float = Z_EXIT, stop_z: float = Z_STOP,
                 max_hold: int = MAX_HOLD_DAYS, regime_threshold: float = VIX_REGIME_THRESHOLD):
        self.exit_z = exit_z
        self.stop_z = stop_z
        self.max_hold = max_hold
        self.regime_threshold = regime_threshold

    def check_exit(self, position: dict, current_z: float, current_price: float,
                   vix_spot: float, account_value: float = 0.0,
                   daily_pnl: float = 0.0, verbose: bool = VERBOSE) -> dict:
        """
        Check all six exit conditions for an open directional position.

        Args:
            position: dict with 'direction', 'entry_date', 'entry_price', 'contracts'
            current_z: current VIX spot z-score
            current_price: current VX1 price
            vix_spot: current VIX spot level
            account_value: net liquidation
            daily_pnl: cumulative daily P&L
        """
        direction = position['direction']  # -1 = short VX1, +1 = long VX1
        entry_date = position['entry_date']
        days_held = (datetime.now() - entry_date).days
        contracts = position['contracts']

        result = {'should_exit': False, 'action': '', 'reason': '', 'details': '', 'reduce_to': 0}

        # Layer 1: Graduated profit-taking — close more as Z reverts toward 0
        entry_z = position.get('entry_z', 0)
        original_contracts = position.get('original_contracts', contracts)
        tiers_hit = position.get('profit_tiers_hit', [])

        # z_ratio = how much of entry_z remains (1.0 = no reversion, 0.0 = fully reverted)
        if abs(entry_z) > 0:
            if direction < 0:  # short: Z should drop toward 0
                z_ratio = current_z / entry_z
            else:  # long: entry_z is negative, Z should rise toward 0
                z_ratio = current_z / entry_z
        else:
            z_ratio = 0.0

        for tier_ratio, cum_close_pct in PROFIT_TAKE_TIERS:
            if tier_ratio in tiers_hit:
                continue
            if z_ratio <= tier_ratio:
                target = max(0, int(original_contracts * (1.0 - cum_close_pct)))
                if target <= 0:
                    # Final tier — full exit
                    result = {'should_exit': True, 'action': 'EXIT', 'reason': 'SIGNAL_EXIT',
                              'details': f'Z={current_z:.2f} | Z-ratio={z_ratio:.2f} <= {tier_ratio} → close 100%',
                              'reduce_to': 0}
                elif target < contracts:
                    # Partial close
                    result = {'should_exit': True, 'action': 'REDUCE', 'reason': 'PROFIT_TAKE',
                              'details': f'Z={current_z:.2f} | Z-ratio={z_ratio:.2f} <= {tier_ratio} → '
                                         f'close {cum_close_pct:.0%} of {original_contracts} (keep {target})',
                              'reduce_to': target}
                # Mark tier as hit
                tiers_hit.append(tier_ratio)
                position['profit_tiers_hit'] = tiers_hit
                break  # only trigger one tier per check

        # Layer 2: Stop loss — relative to entry z-score (entry_z + Z_STOP offset)
        entry_z = position.get('entry_z', 0)
        is_override = position.get('basis_override', False)
        stop_offset = Z_STOP_OVERRIDE if is_override else self.stop_z
        # Tighten stop for each pyramid add
        pyramid_adds = position.get('pyramid_adds', 0)
        stop_offset *= (PYRAMID_STOP_DECAY ** pyramid_adds)
        stop_short = entry_z + stop_offset    # e.g. override: 1.5 + 1.0 = 2.5; normal: 0.8 + 1.5 = 2.3
        stop_long = -(abs(entry_z) + stop_offset)

        if not result['should_exit'] and direction < 0 and current_z >= stop_short:
            result = {'should_exit': True, 'action': 'EXIT', 'reason': 'STOP_LOSS',
                      'details': f'Z={current_z:.2f} >= {stop_short:.2f} (entry {entry_z:.2f} + stop {stop_offset}{"⚡override" if is_override else ""})',
                      'reduce_to': 0}

        elif not result['should_exit'] and direction > 0 and current_z <= stop_long:
            result = {'should_exit': True, 'action': 'EXIT', 'reason': 'STOP_LOSS',
                      'details': f'Z={current_z:.2f} <= {stop_long:.2f} (entry {entry_z:.2f} - stop {stop_offset}{"⚡override" if is_override else ""})',
                      'reduce_to': 0}

        # Layer 3: Absolute $ stop
        elif account_value > 0:
            unrealized_pnl = direction * (current_price - position['entry_price']) \
                             * contracts * CAPITAL_PER_CONTRACT
            max_loss = account_value * MAX_LOSS_PCT
            if unrealized_pnl <= -max_loss:
                result = {'should_exit': True, 'action': 'EXIT', 'reason': 'ABS_STOP_LOSS',
                          'details': f'Unrealized loss ${unrealized_pnl:,.2f} exceeds '
                                     f'{MAX_LOSS_PCT:.0%} of account (${-max_loss:,.2f})',
                          'reduce_to': 0}

        # Layer 4: Max hold
        if not result['should_exit'] and days_held >= self.max_hold:
            result = {'should_exit': True, 'action': 'EXIT', 'reason': 'MAX_HOLD',
                      'details': f'Held {days_held} days >= {self.max_hold} max hold',
                      'reduce_to': 0}

        # Layer 5: Daily max loss
        if not result['should_exit'] and account_value > 0:
            daily_limit = account_value * DAILY_MAX_LOSS_PCT
            if daily_pnl <= -daily_limit:
                result = {'should_exit': True, 'action': 'EXIT', 'reason': 'DAILY_MAX_LOSS',
                          'details': f'Daily loss ${daily_pnl:,.2f} exceeds '
                                     f'{DAILY_MAX_LOSS_PCT:.0%} of account',
                          'reduce_to': 0}

        # Layer 6: Regime — only affects LONG positions
        if not result['should_exit'] and direction > 0:
            if vix_spot >= self.regime_threshold:
                result = {'should_exit': True, 'action': 'EXIT', 'reason': 'REGIME_EXIT',
                          'details': f'VIX={vix_spot:.1f} >= {self.regime_threshold} (exit long)',
                          'reduce_to': 0}
            elif (vix_spot >= VIX_REGIME_REDUCE_THRESHOLD
                  and not position.get('regime_reduced', False)):
                target = max(1, int(contracts * REGIME_REDUCE_FACTOR))
                if target < contracts:
                    result = {'should_exit': True, 'action': 'REDUCE', 'reason': 'REGIME_REDUCE',
                              'details': f'VIX={vix_spot:.1f} >= {VIX_REGIME_REDUCE_THRESHOLD} '
                                         f'(reducing long {contracts} → {target})',
                              'reduce_to': target}

        if verbose and result['should_exit']:
            dir_str = 'SHORT' if direction < 0 else 'LONG'
            print(f"[EXIT] {dir_str} VX1 | {result['reason']}: {result['details']}")

        return result


# =============================================================================
# Position Tracker — manages open position state with persistence
# =============================================================================
class PositionTracker:
    """
    Tracks the current open directional VX1 position.
    Persists state to disk so positions survive restarts.
    """

    def __init__(self, state_file: str = "vix_position_state.json"):
        self._state_file = Path(state_file)
        self.position = None
        self.trade_log = []
        self._load_state()

    def _load_state(self):
        if not self._state_file.exists():
            return
        try:
            with open(self._state_file, 'r') as f:
                state = json.load(f)
            if state.get('position'):
                pos = state['position']
                pos['entry_date'] = datetime.fromisoformat(pos['entry_date'])
                self.position = pos
            self.trade_log = state.get('trade_log', [])
            print(f"[TRACKER] Loaded state. Position: {'OPEN' if self.position else 'FLAT'}, "
                  f"Trade history: {len(self.trade_log)} trades.")
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            print(f"[WARN] Corrupted state file, starting fresh: {e}")

    def _save_state(self):
        state = {'position': None, 'trade_log': self.trade_log}
        if self.position:
            pos_copy = self.position.copy()
            pos_copy['entry_date'] = pos_copy['entry_date'].isoformat()
            state['position'] = pos_copy
        with open(self._state_file, 'w') as f:
            json.dump(state, f, indent=2, default=str)

    def open_position(self, direction: int, entry_price: float, entry_z: float,
                      contracts: int, basis_override: bool = False):
        """Record a new position opening."""
        self.position = {
            'direction': direction,       # -1 = short VX1, +1 = long VX1
            'entry_date': datetime.now(),
            'entry_price': entry_price,
            'entry_z': entry_z,
            'contracts': contracts,
            'original_contracts': contracts,  # for graduated profit-taking
            'prev_price': entry_price,
            'regime_reduced': False,
            'basis_override': basis_override,
            'profit_tiers_hit': []            # track which tiers already triggered
        }
        self._save_state()

        dir_str = 'SHORT' if direction < 0 else 'LONG'
        print(f"[POSITION] Opened {dir_str} VX1 × {contracts} | "
              f"Entry: {entry_price:.2f} | Z: {entry_z:.3f}")

    def _make_trade_record(self, pos: dict, exit_price: float, exit_z: float,
                           contracts: int, reason: str) -> dict:
        """Build a trade log entry from position state."""
        pnl_per_point = pos['direction'] * (exit_price - pos['entry_price'])
        total_pnl = pnl_per_point * contracts * CAPITAL_PER_CONTRACT
        days_held = (datetime.now() - pos['entry_date']).days
        return {
            'entry_date': pos['entry_date'].isoformat(),
            'exit_date': datetime.now().isoformat(),
            'direction': 'SHORT' if pos['direction'] < 0 else 'LONG',
            'entry_price': pos['entry_price'],
            'exit_price': exit_price,
            'entry_z': pos.get('entry_z', 0),
            'exit_z': exit_z,
            'contracts': contracts,
            'pnl': round(total_pnl, 2),
            'pnl_per_point': round(pnl_per_point, 4),
            'days_held': days_held,
            'exit_reason': reason
        }

    def close_position(self, exit_price: float, exit_z: float, reason: str) -> dict:
        """Record position close and log the trade."""
        if not self.position:
            return {}

        trade = self._make_trade_record(self.position, exit_price, exit_z,
                                         self.position['contracts'], reason)
        self.trade_log.append(trade)
        self.position = None
        self._save_state()

        print(f"[CLOSED] {trade['direction']} VX1 | PnL: ${trade['pnl']:,.2f} | "
              f"Held: {trade['days_held']}d | Reason: {reason}")
        return trade

    def add_to_position(self, add_contracts: int, current_price: float, current_z: float):
        """
        Pyramid: add contracts to existing position.
        Recalculates blended entry_price, entry_z, and tightens stop via pyramid_adds counter.
        """
        if not self.position:
            return

        old_qty = self.position['contracts']
        new_qty = old_qty + add_contracts
        old_price = self.position['entry_price']
        old_z = self.position['entry_z']

        # Blended averages
        blended_price = (old_price * old_qty + current_price * add_contracts) / new_qty
        blended_z = (old_z * old_qty + current_z * add_contracts) / new_qty

        self.position['entry_price'] = round(blended_price, 4)
        self.position['entry_z'] = round(blended_z, 6)
        self.position['contracts'] = new_qty
        self.position['original_contracts'] = new_qty  # reset for profit-taking tiers
        self.position['profit_tiers_hit'] = []          # reset profit tiers
        self.position['prev_price'] = current_price
        self.position['last_add_time'] = datetime.now().isoformat()
        self.position['pyramid_adds'] = self.position.get('pyramid_adds', 0) + 1
        self.position['last_add_z'] = current_z

        self._save_state()

        dir_str = 'SHORT' if self.position['direction'] < 0 else 'LONG'
        print(f"[PYRAMID] {dir_str} VX1: +{add_contracts} → {new_qty} contracts | "
              f"Blended entry: {blended_price:.2f} (z={blended_z:.3f}) | "
              f"Add #{self.position['pyramid_adds']}")

    def update_mtm(self, current_price: float) -> float:
        """Update daily mark-to-market. Returns today's MtM P&L."""
        if not self.position:
            return 0.0
        prev = self.position.get('prev_price', self.position['entry_price'])
        daily_pnl = self.position['direction'] * (current_price - prev) * \
                     self.position['contracts'] * CAPITAL_PER_CONTRACT
        self.position['prev_price'] = current_price
        self._save_state()
        return daily_pnl

    def reduce_position(self, new_contracts: int, current_price: float, reason: str = 'REGIME_REDUCE'):
        """Reduce position size and log the partial close as a trade."""
        if not self.position or new_contracts >= self.position['contracts']:
            return

        closed_qty = self.position['contracts'] - new_contracts
        trade = self._make_trade_record(self.position, current_price, 0,
                                         closed_qty, reason)
        self.trade_log.append(trade)

        old = self.position['contracts']
        self.position['contracts'] = new_contracts
        if reason == 'REGIME_REDUCE':
            self.position['regime_reduced'] = True
        self.position['prev_price'] = current_price
        self._save_state()

        dir_str = 'SHORT' if self.position['direction'] < 0 else 'LONG'
        print(f"[REDUCE] {dir_str} VX1 reduced: {old} → {new_contracts} contracts | "
              f"Partial PnL: ${trade['pnl']:,.2f}")

    def get_todays_realized_pnl(self) -> float:
        today = datetime.now().date().isoformat()
        return sum(t['pnl'] for t in self.trade_log if t['exit_date'][:10] == today)

    def get_unrealized_pnl(self, current_price: float) -> float:
        if not self.position:
            return 0.0
        return (self.position['direction']
                * (current_price - self.position['entry_price'])
                * self.position['contracts'] * CAPITAL_PER_CONTRACT)

    @property
    def is_flat(self) -> bool:
        return self.position is None

    @property
    def is_long(self) -> bool:
        return self.position is not None and self.position['direction'] > 0

    @property
    def is_short(self) -> bool:
        return self.position is not None and self.position['direction'] < 0


# =============================================================================
# Strategy Engine — orchestrates directional VX1 trading
# =============================================================================
class VIXStrategyEngine:
    """
    Main orchestrator for directional VIX futures trading.
    1. Fetch VIX spot + VX1 price + volume
    2. Update history
    3. If in position → six-layer exit check
    4. If flat → check for directional entry signal
    5. Execute single-leg VX1 orders
    """

    def __init__(self, ibkr: IBKR, verbose: bool = VERBOSE):
        self.ibkr = ibkr
        self.contract_mgr = VIXContractManager(ibkr)
        self.data_engine = VIXDataEngine(ibkr, self.contract_mgr)
        self.signal_engine = VIXSignalEngine(lookback=Z_LOOKBACK,
                                              entry_short=Z_ENTRY_SHORT,
                                              entry_long=Z_ENTRY_LONG)
        self.risk_mgr = VIXRiskManager()
        self.tracker = PositionTracker()
        self.news_engine = VIXNewsEngine(verbose=verbose) if NEWS_SENTIMENT_ENABLED else None
        self.live_chart = LiveTerminalChart(max_ticks=60)
        self.verbose = verbose

        self.vx1_contract = None
        self._last_entry_date = None  # Cooldown: max 1 entry per day
        self.data_engine.load_state()
        self._reconciled = False  # run once on first cycle

    def _reconcile_positions(self):
        """Compare JSON state vs TWS actual positions. Warn on mismatch."""
        if self._reconciled:
            return
        self._reconciled = True

        # Get all positions from TWS
        tws_positions = self.ibkr.get_positions()

        # Find VX futures in TWS (localSymbol starts with "VX")
        tws_vx_qty = 0
        tws_vx_symbols = []
        for symbol, info in tws_positions.items():
            if symbol.startswith("VX") and not symbol.startswith("VXX"):
                qty = int(info['qty'])
                if qty != 0:
                    tws_vx_qty += qty
                    tws_vx_symbols.append(f"{symbol}={qty:+d}")

        # Get JSON state
        json_pos = self.tracker.position
        if json_pos:
            json_qty = json_pos['contracts'] * json_pos['direction']  # signed
            json_dir = 'LONG' if json_pos['direction'] > 0 else 'SHORT'
        else:
            json_qty = 0
            json_dir = 'FLAT'

        # Compare
        if tws_vx_qty == json_qty:
            print(f"[RECONCILE] ✓ TWS and JSON agree: {json_dir} {abs(json_qty)} contracts")
        else:
            print(f"\n{'!'*60}")
            print(f"  ⚠ POSITION MISMATCH DETECTED")
            print(f"  TWS actual:  {tws_vx_symbols if tws_vx_symbols else 'FLAT'} (net {tws_vx_qty:+d})")
            print(f"  JSON state:  {json_dir} {abs(json_qty)} (net {json_qty:+d})")
            print(f"  → Fix vix_position_state.json before trading!")
            print(f"{'!'*60}\n")

    def _resolve_contract(self):
        """Resolve front month VIX futures contract."""
        self.vx1_contract = self.contract_mgr.get_front_month()
        if not self.vx1_contract:
            raise RuntimeError("Failed to resolve VX1 contract.")

    def _check_roll_needed(self) -> bool:
        if not self.vx1_contract:
            return True
        days = self.contract_mgr.days_to_expiry(self.vx1_contract)
        if days <= 3:
            print(f"[ROLL] VX1 expires in {days} days — re-resolving.")
            return True
        return False

    def _handle_roll(self):
        """
        Handle contract near expiry: close position instead of rolling.
        For a short-term strategy (avg hold 4.7d), it's safer to exit and
        wait for a new signal on the next contract than to force-roll
        during potentially high-volatility expiry periods.
        """
        # If contract not yet resolved (first startup), just resolve it
        if not self.vx1_contract:
            self._resolve_contract()
            return

        if self.tracker.is_flat:
            self._resolve_contract()
            return

        # Close position — don't roll, just exit
        pos = self.tracker.position
        direction = pos['direction']
        contracts = pos['contracts']
        dir_str = 'SHORT' if direction < 0 else 'LONG'

        print(f"\n[ROLL] VX1 near expiry — closing {dir_str} × {contracts} (no roll)")

        close_action = 'BUY' if direction < 0 else 'SELL'
        vx1_price = self.data_engine.fetch_current_price(self.vx1_contract)

        if math.isnan(vx1_price):
            print("[ERROR] Cannot get VX1 price for roll exit. Skipping.")
            return

        try:
            self.ibkr.place_order(self.vx1_contract, close_action, contracts)
            print(f"[EXEC] {close_action} {contracts} VX1 ({self.vx1_contract.localSymbol})")
        except Exception as e:
            print(f"[ERROR] Roll exit order failed: {e}. Position NOT closed.")
            return

        # Compute z for trade log
        vix_history = self.data_engine.vix_history
        current_z = 0.0
        if len(vix_history) >= Z_LOOKBACK + 1:
            z_series = self.signal_engine.compute_zscore(vix_history['vix_spot'])
            current_z = z_series.iloc[-1]

        self.tracker.close_position(vx1_price, current_z, 'EXPIRY_EXIT')

        # Resolve new front month for next signal
        self._resolve_contract()

    def run_daily_cycle(self):
        """Main daily routine. Called ~30 min before market close."""
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        print(f"\n{'='*60}")
        print(f"  VIX Directional — Daily Cycle @ {now}")
        print(f"{'='*60}")

        # Step 0: Resolve or roll contract
        if self._check_roll_needed():
            self._handle_roll()

        # Step 0.5: Reconcile TWS vs JSON state (once per startup)
        self._reconcile_positions()

        # Step 1: Fetch current prices and volume
        vx1_price = self.data_engine.fetch_current_price(self.vx1_contract)
        vix_spot = self.data_engine.fetch_vix_spot()
        vx1_volume = self.data_engine.fetch_latest_volume(self.vx1_contract)

        print(f"[PRICES] VX1: {vx1_price:.2f} | VIX Spot: {vix_spot:.2f}")
        print(f"[VOLUME] VX1: {vx1_volume:,.0f}")

        if math.isnan(vx1_price) or math.isnan(vix_spot):
            print("[ERROR] Price fetch failed. Skipping this cycle.")
            return

        # Step 2: Update history
        self.data_engine.update_today(vix_spot, vx1_price, vx1_volume)

        # Step 3: Ensure sufficient history for z-score
        vix_history = self.data_engine.vix_history
        if len(vix_history) < Z_LOOKBACK + 1:
            print(f"[DATA] Insufficient history ({len(vix_history)}/{Z_LOOKBACK + 1}). "
                  f"Fetching from IBKR...")
            hist = self.data_engine.build_vix_history(self.vx1_contract)
            if not hist.empty:
                self.data_engine.vix_history = hist
                self.data_engine.update_today(vix_spot, vx1_price, vx1_volume)
                vix_history = self.data_engine.vix_history
            if len(vix_history) < Z_LOOKBACK + 1:
                print(f"[WAIT] Still insufficient ({len(vix_history)}/{Z_LOOKBACK + 1}). Retry next cycle.")
                return

        z_series = self.signal_engine.compute_zscore(vix_history['vix_spot'])
        current_z = z_series.iloc[-1]
        account_value = self.ibkr.get_net_liquidation()

        # Regime shift detection for pyramid gating
        regime_shift = self.signal_engine.detect_regime_shift(
            vix_history['vix_spot'], verbose=self.verbose)

        # Step 4: Daily MtM
        if not self.tracker.is_flat:
            daily_mtm = self.tracker.update_mtm(vx1_price)
            print(f"[MTM] Daily P&L: ${daily_mtm:,.2f}")

        # Step 5: Exit checks
        if not self.tracker.is_flat:
            daily_pnl = (self.tracker.get_todays_realized_pnl()
                         + self.tracker.get_unrealized_pnl(vx1_price))

            exit_result = self.risk_mgr.check_exit(
                self.tracker.position, current_z, vx1_price, vix_spot,
                account_value=account_value, daily_pnl=daily_pnl,
                verbose=self.verbose)

            if exit_result['should_exit']:
                if exit_result.get('action') == 'REDUCE':
                    self._execute_reduce(vx1_price, exit_result['reduce_to'], exit_result.get('reason', 'REDUCE'))
                else:
                    self._execute_exit(vx1_price, current_z, exit_result['reason'])
                return

            # Step 5b: Pyramid check (only if no exit triggered)
            if self._check_pyramid(current_z, vx1_price, regime_shift=regime_shift):
                self._execute_add(current_z, vx1_price)
                return

        # Step 6: Entry check
        if self.tracker.is_flat:
            if account_value > 0:
                daily_realized = self.tracker.get_todays_realized_pnl()
                daily_limit = account_value * DAILY_MAX_LOSS_PCT
                if daily_realized <= -daily_limit:
                    print(f"[BLOCKED] Daily loss ${daily_realized:,.2f} exceeds limit. No entries today.")
                    print(f"{'='*60}\n")
                    return

            signal = self.signal_engine.generate_signal(
                vix_history, vix_spot,
                vx1_price=vx1_price,
                news_engine=self.news_engine,
                verbose=self.verbose)

            if signal['signal'] != 'NO_SIGNAL':
                self._execute_entry(signal, vx1_price, current_z)

        print(f"{'='*60}\n")

    @staticmethod
    def _size_by_zscore(z: float) -> int:
        az = abs(z)
        for threshold, size in POSITION_SIZE_TIERS:
            if az >= threshold:
                return min(size, MAX_CONTRACTS)
        return 1

    def _check_pyramid(self, current_z: float, vx1_price: float,
                       regime_shift: dict = None) -> bool:
        """Check if conditions are met to add to existing position."""
        if not PYRAMID_ENABLED or self.tracker.is_flat:
            return False

        # Block pyramid during regime shift
        if regime_shift and regime_shift.get('detected') and REGIME_SHIFT_BLOCK_PYRAMID:
            if self.verbose:
                print(f"[PYRAMID] Blocked — regime shift detected")
            return False

        pos = self.tracker.position
        pyramid_adds = pos.get('pyramid_adds', 0)

        # Max adds reached
        if pyramid_adds >= PYRAMID_MAX_ADDS:
            return False

        # Cooldown check
        last_add = pos.get('last_add_time')
        if last_add:
            hours_since = (datetime.now() - datetime.fromisoformat(last_add)).total_seconds() / 3600
            if hours_since < PYRAMID_COOLDOWN_HOURS:
                return False
        else:
            # Cooldown from initial entry
            hours_since = (datetime.now() - pos['entry_date']).total_seconds() / 3600
            if hours_since < PYRAMID_COOLDOWN_HOURS:
                return False

        # Z-score gap check: must have moved further in the trade direction
        direction = pos['direction']
        last_z = pos.get('last_add_z', pos['entry_z'])
        if direction < 0:  # SHORT: z should have increased
            if current_z - last_z < PYRAMID_MIN_Z_GAP:
                return False
        else:  # LONG: z should have decreased (more negative)
            if last_z - current_z < PYRAMID_MIN_Z_GAP:
                return False

        # Target size from tiers must be larger than current
        target_size = self._size_by_zscore(current_z)
        if target_size <= pos['contracts']:
            return False

        # Max contracts cap
        if pos['contracts'] >= MAX_CONTRACTS:
            return False

        return True

    def _execute_add(self, current_z: float, vx1_price: float):
        """Execute a pyramid add to existing position."""
        pos = self.tracker.position
        target_size = self._size_by_zscore(current_z)
        add_qty = min(target_size - pos['contracts'], MAX_CONTRACTS - pos['contracts'])

        if add_qty <= 0:
            return

        direction = pos['direction']
        dir_str = 'SHORT' if direction < 0 else 'LONG'
        action = 'SELL' if direction < 0 else 'BUY'
        add_num = pos.get('pyramid_adds', 0) + 1

        print(f"\n{'='*60}")
        print(f"  PYRAMID ADD #{add_num}: {dir_str} VX1 +{add_qty} "
              f"({pos['contracts']} → {pos['contracts'] + add_qty})")
        print(f"  VX1: {vx1_price:.2f} | Z: {current_z:.3f}")
        print(f"{'='*60}")

        if REQUIRE_APPROVAL:
            print("\a")
            user_input = input("Execute add? (y/n): ")
            if user_input.lower() != 'y':
                print("[CANCELLED] Add not executed.")
                return

        try:
            self.ibkr.place_order(self.vx1_contract, action, add_qty)
            print(f"[EXEC] {action} {add_qty} VX1 ({self.vx1_contract.localSymbol})")
        except Exception as e:
            print(f"[ERROR] Add order failed: {e}. Position NOT modified.")
            return

        self.tracker.add_to_position(add_qty, vx1_price, current_z)

    def _execute_entry(self, signal: dict, vx1_price: float, current_z: float):
        """Execute a new directional VX1 entry."""
        direction = -1 if signal['signal'] == 'SHORT' else 1
        dir_str = 'SHORT' if direction < 0 else 'LONG'
        contracts = self._size_by_zscore(current_z)
        action = 'SELL' if direction < 0 else 'BUY'

        print(f"\n{'='*60}")
        print(f"  NEW TRADE: {dir_str} VX1 × {contracts}")
        print(f"  Action: {action} {contracts} VX1 ({self.vx1_contract.localSymbol})")
        print(f"  VX1 Price: {vx1_price:.2f} | Z-Score: {current_z:.3f}")
        print(f"  Notional: ${contracts * CAPITAL_PER_CONTRACT:,.0f} ({contracts}×${CAPITAL_PER_CONTRACT}/pt)")
        print(f"{'='*60}")

        if REQUIRE_APPROVAL:
            print("\a")
            user_input = input("Execute this trade? (y/n): ")
            if user_input.lower() != 'y':
                print("[CANCELLED] Trade not executed.")
                return

        try:
            self.ibkr.place_order(self.vx1_contract, action, contracts)
            print(f"[EXEC] {action} {contracts} VX1 ({self.vx1_contract.localSymbol})")
        except Exception as e:
            print(f"[ERROR] Order failed: {e}. Position NOT opened.")
            return

        self.tracker.open_position(direction, vx1_price, current_z, contracts,
                                    basis_override=signal.get('basis_override', False))

    def _execute_reduce(self, current_price: float, target_contracts: int, reason: str = 'REGIME_REDUCE'):
        """Partially close VX1 position."""
        if self.tracker.is_flat:
            return

        pos = self.tracker.position
        direction = pos['direction']
        contracts_to_close = pos['contracts'] - target_contracts

        if contracts_to_close <= 0:
            return

        close_action = 'BUY' if direction < 0 else 'SELL'

        print(f"\n[REDUCE] Closing {contracts_to_close} of {pos['contracts']} contracts ({reason})")

        try:
            self.ibkr.place_order(self.vx1_contract, close_action, contracts_to_close)
            print(f"[EXEC] {close_action} {contracts_to_close} VX1 ({self.vx1_contract.localSymbol})")
        except Exception as e:
            print(f"[ERROR] Reduce order failed: {e}. Position unchanged.")
            return

        self.tracker.reduce_position(target_contracts, current_price, reason)

    def _execute_exit(self, current_price: float, current_z: float, reason: str):
        """Close the open VX1 position."""
        pos = self.tracker.position
        direction = pos['direction']
        contracts = pos['contracts']
        close_action = 'BUY' if direction < 0 else 'SELL'

        print(f"\n[EXIT] Closing position — reason: {reason}")

        try:
            self.ibkr.place_order(self.vx1_contract, close_action, contracts)
            print(f"[EXEC] {close_action} {contracts} VX1 ({self.vx1_contract.localSymbol})")
        except Exception as e:
            print(f"[ERROR] Exit order failed: {e}. Position NOT closed in tracker.")
            return

        self.tracker.close_position(current_price, current_z, reason)

    def routine_risk_check(self):
        """Intraday monitoring — exit checks when in position, entry checks when flat."""
        if not self.vx1_contract:
            return

        vx1_price = self.data_engine.fetch_current_price(self.vx1_contract)
        vix_spot = self.data_engine.fetch_vix_spot()

        if math.isnan(vx1_price) or math.isnan(vix_spot):
            return

        vix_history = self.data_engine.vix_history
        if len(vix_history) < Z_LOOKBACK + 1:
            return

        # Use live VIX spot for intraday z-score (not stale history)
        live_history = vix_history.copy()
        live_history.loc[live_history.index[-1], 'vix_spot'] = vix_spot
        z_series = self.signal_engine.compute_zscore(live_history['vix_spot'])
        current_z = z_series.iloc[-1] if len(z_series) > 0 else 0.0

        # Regime shift detection for pyramid gating
        regime_shift = self.signal_engine.detect_regime_shift(
            live_history['vix_spot'], verbose=False)

        # Record tick for live chart
        self.live_chart.add_tick(vix_spot, vx1_price, current_z)
        # Draw chart periodically to avoid terminal spam
        if len(self.live_chart.ticks_time) % CHART_EVERY_N_TICKS == 0:
            self.live_chart.draw(position=self.tracker.position)

        # === In position → risk check ===
        if not self.tracker.is_flat:
            account_value = self.ibkr.get_net_liquidation()
            daily_pnl = (self.tracker.get_todays_realized_pnl()
                         + self.tracker.get_unrealized_pnl(vx1_price))

            regime_str = " ⚠REGIME_SHIFT" if regime_shift['detected'] else ""
            if self.verbose:
                now = datetime.now().strftime('%H:%M:%S')
                dir_str = 'SHORT' if self.tracker.is_short else 'LONG'
                print(f"[{now}] Risk check | VX1: {vx1_price:.2f} | "
                      f"Z: {current_z:.3f} | VIX: {vix_spot:.2f} | Pos: {dir_str} | "
                      f"Daily PnL: ${daily_pnl:,.2f}{regime_str}")

            exit_result = self.risk_mgr.check_exit(
                self.tracker.position, current_z, vx1_price, vix_spot,
                account_value=account_value, daily_pnl=daily_pnl,
                verbose=self.verbose)

            if exit_result['should_exit']:
                if exit_result.get('action') == 'REDUCE':
                    self._execute_reduce(vx1_price, exit_result['reduce_to'], exit_result.get('reason', 'REDUCE'))
                else:
                    self._execute_exit(vx1_price, current_z, exit_result['reason'])
            elif self._check_pyramid(current_z, vx1_price, regime_shift=regime_shift):
                self._execute_add(current_z, vx1_price)
            return

        # === Flat → intraday entry check (max 1 per day) ===
        today = datetime.now().date()
        if self._last_entry_date == today:
            return  # Already entered today, cooldown

        # Daily loss guard
        account_value = self.ibkr.get_net_liquidation()
        if account_value > 0:
            daily_realized = self.tracker.get_todays_realized_pnl()
            if daily_realized <= -(account_value * DAILY_MAX_LOSS_PCT):
                return

        signal = self.signal_engine.generate_signal(
            live_history, vix_spot,
            vx1_price=vx1_price,
            news_engine=self.news_engine,
            verbose=False)

        if self.verbose:
            now = datetime.now().strftime('%H:%M:%S')
            basis = vx1_price - vix_spot
            basis_str = f"contango {basis:+.2f}" if basis >= 0 else f"bkwd {basis:+.2f}"
            sig_str = signal['signal'] if signal['signal'] != 'NO_SIGNAL' else 'NO_SIGNAL'
            print(f"[{now}] FLAT | VIX: {vix_spot:.2f} | VX1: {vx1_price:.2f} | "
                  f"Z: {current_z:.3f} | {basis_str} | {sig_str}")

        if signal['signal'] != 'NO_SIGNAL':
            self._execute_entry(signal, vx1_price, current_z)
            if not self.tracker.is_flat:  # Entry succeeded
                self._last_entry_date = today


# =============================================================================
# Automation Bot — daily scheduler
# =============================================================================
class VIXAutomationBot:
    """
    Schedules:
    - Daily signal check at DAILY_SIGNAL_TIME (default 15:30 EST)
    - Intraday risk monitoring every N minutes during market hours
    """

    def __init__(self, strategy_engine: VIXStrategyEngine):
        self.engine = strategy_engine
        self.ibkr = strategy_engine.ibkr

    def is_market_open(self) -> bool:
        """VIX futures trade ~23h: 6:00 PM ET – 4:15 PM ET next day (break 4:15-4:30 PM).
        Risk monitoring covers full electronic + regular session."""
        tz = pytz.timezone('US/Eastern')
        now = datetime.now(tz)
        # No trading on Saturday; Sunday opens at 6 PM ET
        if now.weekday() == 5:  # Saturday
            return False
        if now.weekday() == 6:  # Sunday: only after 6 PM ET
            return now.hour >= 18
        # Mon-Fri: closed 4:15 PM – 6:00 PM ET
        if now.hour == 16 and now.minute >= 15:
            return False
        if now.hour == 17:
            return False
        return True

    def _job_daily_signal(self):
        if self.is_market_open():
            print(f"\n{'#'*60}")
            print(f"  SCHEDULED DAILY SIGNAL CHECK")
            print(f"{'#'*60}")
            self.engine.run_daily_cycle()
        else:
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print(f"[{now}] Daily signal job skipped — market closed.")

    def _job_friday_close(self):
        """Close all positions before weekend to avoid unmonitored gap risk."""
        if self.engine.tracker.is_flat:
            print(f"[FRIDAY CLOSE] No position — nothing to close.")
            return

        print(f"\n{'#'*60}")
        print(f"  FRIDAY AUTO-CLOSE — Clearing positions before weekend")
        print(f"{'#'*60}")

        vx1_price = self.engine.data_engine.fetch_current_price(self.engine.vx1_contract)
        z_series = self.engine.signal_engine.compute_zscore(
            self.engine.data_engine.vix_history['vix_spot'])
        current_z = z_series.iloc[-1] if len(z_series) > 0 else 0.0

        self.engine._execute_exit(vx1_price, current_z, 'FRIDAY_CLOSE')

    def _job_risk_monitor(self):
        if self.is_market_open():
            self.engine.routine_risk_check()

    def start(self, risk_interval_minutes: int = RISK_INTERVAL_MINUTES):
        for day in ['monday', 'tuesday', 'wednesday', 'thursday', 'friday']:
            getattr(schedule.every(), day).at(
                DAILY_SIGNAL_TIME, "America/New_York"
            ).do(self._job_daily_signal)

        schedule.every(risk_interval_minutes).minutes.do(self._job_risk_monitor)

        if FRIDAY_CLOSE_ENABLED:
            schedule.every().friday.at(FRIDAY_CLOSE_TIME, "America/New_York").do(self._job_friday_close)

        print(f"\nVIX Directional Bot Initialized.")
        print(f"   - Daily signal:  Mon-Fri at {DAILY_SIGNAL_TIME} EST")
        if FRIDAY_CLOSE_ENABLED:
            print(f"   - Friday close:  Fri at {FRIDAY_CLOSE_TIME} EST")
        else:
            print(f"   - Friday close:  DISABLED (holding over weekend)")
        print(f"   - Risk monitor:  Every {risk_interval_minutes} min during market hours")
        print(f"   - Position: {'OPEN' if not self.engine.tracker.is_flat else 'FLAT'}")
        print(f"   Waiting for scheduled events...\n")

        try:
            while True:
                schedule.run_pending()
                try:
                    self.ibkr.ib.sleep(1)
                except Exception:
                    print("[WARN] IBKR connection lost. Attempting reconnect...")
                    try:
                        self.ibkr.ib.disconnect()
                        self.ibkr.connect(client_id=np.random.randint(100000, 999999), isTWS=True)
                        self.engine._resolve_contract()
                        print("[OK] Reconnected to IBKR. Contract re-qualified.")
                    except Exception as e:
                        print(f"[ERROR] Reconnect failed: {e}. Retrying in 30s...")
                        time.sleep(30)
        except KeyboardInterrupt:
            print("\nBot stopped manually.")
        finally:
            self.engine.data_engine.save_state()
            self.ibkr.disconnect()
            print("Disconnected. Safe shutdown complete.")


# =============================================================================
# Standalone test
# =============================================================================
if __name__ == "__main__":
    ib = IBKR()
    ib.connect(np.random.randint(100000, 999999), isTWS=True)

    engine = VIXStrategyEngine(ibkr=ib, verbose=True)
    print("\n--- Running single daily cycle (test mode) ---")
    engine.run_daily_cycle()

    if engine.tracker.trade_log:
        print(f"\n--- Trade Log ({len(engine.tracker.trade_log)} trades) ---")
        for t in engine.tracker.trade_log:
            print(f"  {t['entry_date'][:10]} -> {t['exit_date'][:10]} | "
                  f"{t['direction']} | PnL: ${t['pnl']:,.2f} | {t['exit_reason']}")

    ib.disconnect()
