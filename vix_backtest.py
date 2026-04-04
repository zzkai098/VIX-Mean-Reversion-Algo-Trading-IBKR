"""
VIX Directional Futures Strategy — Backtest Engine

Trades single-leg VX1 (front month VIX futures) based on VIX spot z-score.
  - VIX spot z > Z_ENTRY_SHORT → SHORT VX1 (VIX too high, mean-revert down)
  - VIX spot z < -Z_ENTRY_LONG  → LONG VX1 (VIX too low, mean-revert up)

Asymmetric thresholds: shorting is easier (contango roll yield helps),
longing needs stronger signal (contango hurts).

P&L = direction * (exit_price - entry_price) * contracts * $1,000

Uses VIX_History.csv (9147 days, 1990-2026) as proxy for VX1 price.
Rolling 1-month (21 trading day) backtests for parameter optimization.
"""

import itertools
import warnings
import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')


# =============================================================================
# Backtest Engine — Directional VIX Futures
# =============================================================================
class VIXDirectionalBacktest:
    """
    Backtests directional VIX futures trading on VIX spot history.
    Short when VIX is high (z > threshold), long when VIX is low (z < -threshold).
    """

    def __init__(self, params: dict):
        self.p = params
        self.trades = []
        self.equity_curve = []

    def run(self, df: pd.DataFrame, initial_capital: float = 500_000) -> dict:
        """
        Run backtest on DataFrame with column 'CLOSE' (VIX spot, proxy for VX1).
        Returns dict with trades, equity_curve, and metrics.
        """
        self.trades = []
        self.equity_curve = []

        p = self.p
        vix = df['CLOSE'].values
        dates = df.index
        n = len(df)

        position = None  # None = flat, dict = open
        capital = initial_capital

        for i in range(p['z_lookback'], n):
            # Rolling z-score on VIX spot
            window = vix[i - p['z_lookback']:i]  # lookback window (excluding today)
            mu = np.mean(window)
            sigma = np.std(window, ddof=1)
            if sigma < 1e-8:
                z = 0.0
            else:
                z = (vix[i] - mu) / sigma

            current_price = vix[i]
            current_date = dates[i]

            # ===== EXIT CHECKS =====
            if position is not None:
                direction = position['direction']
                contracts = position['contracts']
                entry_price = position['entry_price']
                days_held = (current_date - position['entry_date']).days

                unrealized = direction * (current_price - entry_price) * contracts * p['capital_per_contract']

                should_exit = False
                reason = ''
                reduce_to = 0
                action = 'EXIT'

                # Layer 1: Signal exit (take profit — z reverted toward 0)
                if direction < 0 and z <= p['z_exit']:  # Short: VIX dropped back
                    should_exit, reason = True, 'SIGNAL_EXIT'
                elif direction > 0 and z >= -p['z_exit']:  # Long: VIX rose back
                    should_exit, reason = True, 'SIGNAL_EXIT'

                # Layer 2: Z-score stop loss (VIX moved further against us)
                if not should_exit:
                    if direction < 0 and z >= p['z_stop']:  # Short: VIX spiked higher
                        should_exit, reason = True, 'STOP_LOSS'
                    elif direction > 0 and z <= -p['z_stop']:  # Long: VIX crashed lower
                        should_exit, reason = True, 'STOP_LOSS'

                # Layer 3: Absolute $ stop
                if not should_exit and capital > 0:
                    max_loss = capital * p['max_loss_pct']
                    if unrealized <= -max_loss:
                        should_exit, reason = True, 'ABS_STOP_LOSS'

                # Layer 4: Max hold
                if not should_exit and days_held >= p['max_hold_days']:
                    should_exit, reason = True, 'MAX_HOLD'

                # Layer 5: Daily max loss
                if not should_exit and capital > 0:
                    today_realized = sum(t['pnl'] for t in self.trades
                                        if t['exit_date'] == current_date)
                    total_daily = today_realized + unrealized
                    daily_limit = capital * p['daily_max_loss_pct']
                    if total_daily <= -daily_limit:
                        should_exit, reason = True, 'DAILY_MAX_LOSS'

                # Layer 6: Regime — only affects LONG positions
                if not should_exit and direction > 0:
                    if current_price >= p['regime_threshold']:
                        should_exit, reason = True, 'REGIME_EXIT'
                    elif (current_price >= p['regime_reduce_threshold']
                          and not position.get('regime_reduced', False)):
                        target = max(1, int(contracts * p['regime_reduce_factor']))
                        if target < contracts:
                            action = 'REDUCE'
                            reduce_to = target
                            should_exit, reason = True, 'REGIME_REDUCE'

                if should_exit:
                    if action == 'REDUCE':
                        closed_qty = contracts - reduce_to
                        partial_pnl = direction * (current_price - entry_price) * closed_qty * p['capital_per_contract']
                        capital += partial_pnl
                        self.trades.append({
                            'entry_date': position['entry_date'],
                            'exit_date': current_date,
                            'direction': 'SHORT' if direction < 0 else 'LONG',
                            'entry_price': entry_price,
                            'exit_price': current_price,
                            'contracts': closed_qty,
                            'pnl': round(partial_pnl, 2),
                            'days_held': days_held,
                            'exit_reason': reason
                        })
                        position['contracts'] = reduce_to
                        position['regime_reduced'] = True
                    else:
                        pnl = direction * (current_price - entry_price) * contracts * p['capital_per_contract']
                        capital += pnl
                        self.trades.append({
                            'entry_date': position['entry_date'],
                            'exit_date': current_date,
                            'direction': 'SHORT' if direction < 0 else 'LONG',
                            'entry_price': entry_price,
                            'exit_price': current_price,
                            'contracts': contracts,
                            'pnl': round(pnl, 2),
                            'days_held': days_held,
                            'exit_reason': reason
                        })
                        position = None

            # ===== ENTRY CHECK =====
            if position is None:
                # Daily loss guard
                if capital > 0:
                    today_realized = sum(t['pnl'] for t in self.trades
                                        if t['exit_date'] == current_date)
                    if today_realized <= -(capital * p['daily_max_loss_pct']):
                        self.equity_curve.append({'date': current_date, 'equity': capital})
                        continue

                signal = None

                # SHORT signal: VIX too high, expect mean-reversion down
                if z > p['z_entry_short']:
                    signal = 'SHORT'
                # LONG signal: VIX too low, expect mean-reversion up
                elif z < -p['z_entry_long']:
                    # Regime filter only blocks longs (shorting high VIX is fine)
                    if current_price < p['regime_threshold']:
                        signal = 'LONG'

                # Slope confirmation
                if signal is not None and p.get('slope_confirmation', True):
                    if i >= p['slope_lookback']:
                        slope = vix[i] - vix[i - p['slope_lookback']]
                        # SHORT: VIX should be turning down (slope < 0)
                        if signal == 'SHORT' and slope > 0:
                            signal = None
                        # LONG: VIX should be turning up (slope > 0)
                        elif signal == 'LONG' and slope < 0:
                            signal = None

                if signal is not None:
                    direction = -1 if signal == 'SHORT' else 1
                    contracts = self._size_by_zscore(abs(z), p.get('size_tiers'))

                    position = {
                        'direction': direction,
                        'entry_date': current_date,
                        'entry_price': current_price,
                        'entry_z': z,
                        'contracts': contracts,
                        'regime_reduced': False
                    }

            # Track equity
            equity = capital
            if position is not None:
                equity += (position['direction']
                          * (current_price - position['entry_price'])
                          * position['contracts'] * p['capital_per_contract'])
            self.equity_curve.append({'date': current_date, 'equity': equity})

        # Close remaining position at end
        if position is not None:
            pnl = (position['direction']
                   * (vix[-1] - position['entry_price'])
                   * position['contracts'] * p['capital_per_contract'])
            capital += pnl
            self.trades.append({
                'entry_date': position['entry_date'],
                'exit_date': dates[-1],
                'direction': 'SHORT' if position['direction'] < 0 else 'LONG',
                'entry_price': position['entry_price'],
                'exit_price': vix[-1],
                'contracts': position['contracts'],
                'pnl': round(pnl, 2),
                'days_held': (dates[-1] - position['entry_date']).days,
                'exit_reason': 'END_OF_BACKTEST'
            })

        equity_df = pd.DataFrame(self.equity_curve)
        trades_df = pd.DataFrame(self.trades) if self.trades else pd.DataFrame()
        metrics = self._compute_metrics(equity_df, trades_df, initial_capital)

        return {'trades': trades_df, 'equity': equity_df, 'metrics': metrics}

    @staticmethod
    def _size_by_zscore(az: float, tiers=None) -> int:
        if tiers is None:
            tiers = [(2.5, 20), (2.0, 15), (1.5, 10), (1.0, 8), (0.75, 5)]
        for threshold, size in tiers:
            if az >= threshold:
                return size
        return 1

    @staticmethod
    def _compute_metrics(equity_df: pd.DataFrame, trades_df: pd.DataFrame,
                         initial_capital: float) -> dict:
        if equity_df.empty:
            return {}

        equity = equity_df['equity'].values
        final_equity = equity[-1]
        total_return = (final_equity - initial_capital) / initial_capital

        daily_returns = pd.Series(equity).pct_change().dropna()
        n_days = len(equity)
        ann_factor = 252 / max(n_days, 1)

        if daily_returns.std() > 0:
            sharpe = (daily_returns.mean() / daily_returns.std()) * np.sqrt(252)
        else:
            sharpe = 0.0

        downside = daily_returns[daily_returns < 0]
        if len(downside) > 0 and downside.std() > 0:
            sortino = (daily_returns.mean() / downside.std()) * np.sqrt(252)
        else:
            sortino = 0.0

        peak = np.maximum.accumulate(equity)
        drawdown = (equity - peak) / peak
        max_drawdown = drawdown.min()

        in_dd = equity < peak
        if in_dd.any():
            dd_starts = np.diff(np.concatenate(([0], in_dd.astype(int), [0])))
            starts = np.where(dd_starts == 1)[0]
            ends = np.where(dd_starts == -1)[0]
            if len(starts) > 0 and len(ends) > 0:
                max_dd_duration = int((ends - starts[:len(ends)]).max())
            else:
                max_dd_duration = 0
        else:
            max_dd_duration = 0

        calmar = (total_return * ann_factor) / abs(max_drawdown) if max_drawdown != 0 else 0.0

        n_trades = len(trades_df)
        if n_trades > 0:
            wins = trades_df[trades_df['pnl'] > 0]
            losses = trades_df[trades_df['pnl'] <= 0]
            win_rate = len(wins) / n_trades
            avg_pnl = trades_df['pnl'].mean()
            avg_win = wins['pnl'].mean() if len(wins) > 0 else 0
            avg_loss = losses['pnl'].mean() if len(losses) > 0 else 0
            profit_factor = abs(wins['pnl'].sum() / losses['pnl'].sum()) if losses['pnl'].sum() != 0 else float('inf')
            avg_hold = trades_df['days_held'].mean()
            max_win = trades_df['pnl'].max()
            max_loss = trades_df['pnl'].min()
            exit_reasons = trades_df['exit_reason'].value_counts().to_dict()

            # Short vs Long breakdown
            shorts = trades_df[trades_df['direction'] == 'SHORT']
            longs = trades_df[trades_df['direction'] == 'LONG']
            short_pnl = shorts['pnl'].sum() if len(shorts) > 0 else 0
            long_pnl = longs['pnl'].sum() if len(longs) > 0 else 0
            short_win_rate = (len(shorts[shorts['pnl'] > 0]) / len(shorts) * 100) if len(shorts) > 0 else 0
            long_win_rate = (len(longs[longs['pnl'] > 0]) / len(longs) * 100) if len(longs) > 0 else 0
        else:
            win_rate = avg_pnl = avg_win = avg_loss = 0
            profit_factor = 0
            avg_hold = max_win = max_loss = 0
            exit_reasons = {}
            short_pnl = long_pnl = short_win_rate = long_win_rate = 0
            shorts = longs = pd.DataFrame()

        return {
            'initial_capital': initial_capital,
            'final_equity': round(final_equity, 2),
            'total_return': round(total_return * 100, 2),
            'total_return_ann': round(total_return * ann_factor * 100, 2),
            'sharpe': round(sharpe, 3),
            'sortino': round(sortino, 3),
            'calmar': round(calmar, 3),
            'max_drawdown': round(max_drawdown * 100, 2),
            'max_dd_duration_days': int(max_dd_duration),
            'n_days': n_days,
            'n_trades': n_trades,
            'n_shorts': len(shorts),
            'n_longs': len(longs),
            'short_pnl': round(short_pnl, 2),
            'long_pnl': round(long_pnl, 2),
            'short_win_rate': round(short_win_rate, 1),
            'long_win_rate': round(long_win_rate, 1),
            'win_rate': round(win_rate * 100, 1),
            'avg_pnl': round(avg_pnl, 2),
            'avg_win': round(avg_win, 2),
            'avg_loss': round(avg_loss, 2),
            'max_win': round(max_win, 2),
            'max_loss': round(max_loss, 2),
            'profit_factor': round(profit_factor, 2),
            'avg_hold_days': round(avg_hold, 1),
            'exit_reasons': exit_reasons
        }


# =============================================================================
# Rolling 1-Month Parameter Optimization
# =============================================================================
def rolling_optimize(vix_df: pd.DataFrame, initial_capital: float = 500_000) -> pd.DataFrame:
    """
    For each parameter combo, run rolling 1-month backtests across the full dataset.
    Score = average Sharpe across all 1-month windows.
    """
    param_grid = {
        'z_lookback':           [10, 15, 20],
        'z_entry_short':        [0.3, 0.5, 0.75],          # More aggressive short entry
        'z_entry_long':         [1.0, 1.5, 2.0],           # Wider long entry range
        'z_exit':               [-0.3, 0.0, 0.3],          # Earlier/later take profit
        'z_stop':               [1.5, 2.0, 2.5],           # Tighter stop options
        'slope_lookback':       [2, 3, 5],
        'max_hold_days':        [3, 5, 7],                  # Faster turnover
        'slope_confirmation':   [True, False],              # Toggle slope filter
    }

    fixed = {
        'max_loss_pct': 0.03,
        'daily_max_loss_pct': 0.08,
        'regime_threshold': 35,
        'regime_reduce_threshold': 30,
        'regime_reduce_factor': 0.5,
        'capital_per_contract': 1000,
        'size_tiers': [(2.5, 20), (2.0, 15), (1.5, 10), (1.0, 8), (0.75, 5)],
    }

    keys = list(param_grid.keys())
    values = list(param_grid.values())
    combos = list(itertools.product(*values))

    # Rolling windows: 21 trading days each
    window_size = 21
    warmup = 60
    n = len(vix_df)

    windows = []
    i = warmup
    while i + window_size <= n:
        windows.append((max(0, i - warmup), i + window_size))
        i += window_size

    n_windows = len(windows)
    print(f"Rolling optimization: {len(combos)} param combos × {n_windows} monthly windows")
    print(f"Data: {vix_df.index[0].date()} → {vix_df.index[-1].date()} ({n} days)\n")

    results = []

    for idx, combo in enumerate(combos):
        params = {k: v for k, v in zip(keys, combo)}
        params.update(fixed)

        monthly_returns = []
        monthly_sharpes = []
        monthly_trades = []
        monthly_wins = []
        monthly_max_dds = []

        for w_start, w_end in windows:
            window_df = vix_df.iloc[w_start:w_end]
            bt = VIXDirectionalBacktest(params)
            result = bt.run(window_df, initial_capital)
            m = result['metrics']

            if m:
                monthly_returns.append(m['total_return'])
                monthly_sharpes.append(m['sharpe'])
                monthly_trades.append(m['n_trades'])
                monthly_wins.append(m['win_rate'])
                monthly_max_dds.append(m['max_drawdown'])

        if not monthly_returns:
            continue

        record = {k: v for k, v in zip(keys, combo)}
        record['avg_return'] = np.mean(monthly_returns)
        record['avg_sharpe'] = np.mean(monthly_sharpes)
        record['median_sharpe'] = np.median(monthly_sharpes)
        record['min_return'] = np.min(monthly_returns)
        record['max_return'] = np.max(monthly_returns)
        record['avg_max_dd'] = np.mean(monthly_max_dds)
        record['worst_dd'] = np.min(monthly_max_dds)
        record['total_trades'] = np.sum(monthly_trades)
        record['avg_trades_per_month'] = np.mean(monthly_trades)
        record['avg_win_rate'] = np.mean([w for w in monthly_wins if w > 0]) if any(w > 0 for w in monthly_wins) else 0
        record['pct_months_profitable'] = np.mean([1 if r > 0 else 0 for r in monthly_returns]) * 100
        record['n_windows'] = len(monthly_returns)
        results.append(record)

        if (idx + 1) % 200 == 0:
            best_so_far = max(results, key=lambda r: r['avg_return'])
            print(f"  {idx + 1}/{len(combos)} done... "
                  f"best avg return: {best_so_far['avg_return']:+.2f}%")

    return pd.DataFrame(results)


# =============================================================================
# Pretty Print
# =============================================================================
def print_metrics(metrics: dict, title: str = "Backtest Results"):
    print(f"\n{'='*65}")
    print(f"  {title}")
    print(f"{'='*65}")
    print(f"  Period:              {metrics['n_days']} trading days")
    print(f"  Initial Capital:     ${metrics['initial_capital']:>12,.2f}")
    print(f"  Final Equity:        ${metrics['final_equity']:>12,.2f}")
    print(f"  Total Return:         {metrics['total_return']:>+11.2f}%")
    print(f"  Annualized Return:    {metrics['total_return_ann']:>+11.2f}%")
    print(f"{'─'*65}")
    print(f"  Sharpe Ratio:         {metrics['sharpe']:>11.3f}")
    print(f"  Sortino Ratio:        {metrics['sortino']:>11.3f}")
    print(f"  Calmar Ratio:         {metrics['calmar']:>11.3f}")
    print(f"  Max Drawdown:         {metrics['max_drawdown']:>+11.2f}%")
    print(f"  Max DD Duration:      {metrics['max_dd_duration_days']:>8d} days")
    print(f"{'─'*65}")
    print(f"  Total Trades:         {metrics['n_trades']:>11d}")
    print(f"    Shorts:             {metrics['n_shorts']:>11d}  (PnL: ${metrics['short_pnl']:>+12,.2f}, Win: {metrics['short_win_rate']:.1f}%)")
    print(f"    Longs:              {metrics['n_longs']:>11d}  (PnL: ${metrics['long_pnl']:>+12,.2f}, Win: {metrics['long_win_rate']:.1f}%)")
    print(f"  Win Rate:             {metrics['win_rate']:>11.1f}%")
    print(f"  Avg P&L / Trade:     ${metrics['avg_pnl']:>12,.2f}")
    print(f"  Avg Win:             ${metrics['avg_win']:>12,.2f}")
    print(f"  Avg Loss:            ${metrics['avg_loss']:>12,.2f}")
    print(f"  Max Win:             ${metrics['max_win']:>12,.2f}")
    print(f"  Max Loss:            ${metrics['max_loss']:>12,.2f}")
    print(f"  Profit Factor:        {metrics['profit_factor']:>11.2f}")
    print(f"  Avg Hold (days):      {metrics['avg_hold_days']:>11.1f}")
    print(f"{'─'*65}")
    print(f"  Exit Reasons:")
    for reason, count in metrics.get('exit_reasons', {}).items():
        print(f"    {reason:<25s} {count:>5d}")
    print(f"{'='*65}\n")


# =============================================================================
# Main
# =============================================================================
if __name__ == "__main__":
    print("=" * 65)
    print("  VIX Directional Futures — Competition Mode Backtest")
    print("  (Optimizing for MAX MONTHLY RETURN)")
    print("=" * 65)

    # Load VIX spot history
    print("\n[1] Loading VIX spot history...")
    vix_df = pd.read_csv("VIX_History.csv", parse_dates=['DATE'], index_col='DATE')
    vix_df.index = pd.to_datetime(vix_df.index)
    vix_df = vix_df.sort_index()

    # Use last 6 years for backtest
    vix_df = vix_df['2020-01-01':]
    print(f"    {len(vix_df)} days: {vix_df.index[0].date()} → {vix_df.index[-1].date()}")
    print(f"    VIX: mean={vix_df['CLOSE'].mean():.2f}, std={vix_df['CLOSE'].std():.2f}, "
          f"range=[{vix_df['CLOSE'].min():.2f}, {vix_df['CLOSE'].max():.2f}]")

    INITIAL_CAPITAL = 500_000

    # =========================================================================
    # Step 2: Rolling 1-month optimization
    # =========================================================================
    print(f"\n{'#'*65}")
    print(f"  Rolling 1-Month Parameter Optimization (6 Years)")
    print(f"{'#'*65}\n")

    results_df = rolling_optimize(vix_df, initial_capital=INITIAL_CAPITAL)
    results_df = results_df.sort_values('avg_return', ascending=False)

    print(f"\n{'='*155}")
    print(f"  Top 15 Parameter Sets (by Average Monthly Return — Competition Mode)")
    print(f"{'='*155}")
    header = (f"{'Rank':>4} | {'Z_LB':>4} | {'Z_ES':>4} | {'Z_EL':>4} | {'Z_EX':>5} | {'Z_ST':>4} | "
              f"{'SL_LB':>5} | {'MH_D':>4} | {'Slope':>5} | {'AvgRet%':>8} | {'AvgShp':>7} | "
              f"{'MinRet%':>8} | {'WorstDD':>8} | "
              f"{'#Trd':>5} | {'Trd/Mo':>6} | {'Win%':>5} | {'%Mon+':>5}")
    print(header)
    print('─' * 155)

    for rank, (_, row) in enumerate(results_df.head(15).iterrows(), 1):
        slope_str = 'Y' if row['slope_confirmation'] else 'N'
        print(f"{rank:>4} | {row['z_lookback']:>4.0f} | {row['z_entry_short']:>4.2f} | "
              f"{row['z_entry_long']:>4.2f} | {row['z_exit']:>+5.2f} | {row['z_stop']:>4.1f} | "
              f"{row['slope_lookback']:>5.0f} | {row['max_hold_days']:>4.0f} | "
              f"{slope_str:>5} | "
              f"{row['avg_return']:>+7.2f}% | {row['avg_sharpe']:>7.3f} | "
              f"{row['min_return']:>+7.2f}% | {row['worst_dd']:>+7.2f}% | "
              f"{row['total_trades']:>5.0f} | {row['avg_trades_per_month']:>5.1f} | "
              f"{row['avg_win_rate']:>4.1f}% | {row['pct_months_profitable']:>4.0f}%")
    print(f"{'='*155}\n")

    # =========================================================================
    # Step 3: Best params — full 6-year backtest
    # =========================================================================
    best_row = results_df.iloc[0]
    best_params = {
        'z_lookback': int(best_row['z_lookback']),
        'z_entry_short': best_row['z_entry_short'],
        'z_entry_long': best_row['z_entry_long'],
        'z_exit': best_row['z_exit'],
        'z_stop': best_row['z_stop'],
        'slope_lookback': int(best_row['slope_lookback']),
        'max_hold_days': int(best_row['max_hold_days']),
        'slope_confirmation': bool(best_row['slope_confirmation']),
        'max_loss_pct': 0.03,
        'daily_max_loss_pct': 0.08,
        'regime_threshold': 35,
        'regime_reduce_threshold': 30,
        'regime_reduce_factor': 0.5,
        'capital_per_contract': 1000,
        'size_tiers': [(2.5, 20), (2.0, 15), (1.5, 10), (1.0, 8), (0.75, 5)],
    }

    print(f"{'#'*65}")
    print(f"  Best Parameters — Full 6-Year Backtest ($500k)")
    print(f"{'#'*65}")

    bt = VIXDirectionalBacktest(best_params)
    full_result = bt.run(vix_df, initial_capital=INITIAL_CAPITAL)
    print_metrics(full_result['metrics'], "FULL 6-YEAR BACKTEST — Directional VIX ($500k)")

    print("Best Parameters:")
    for k in ['z_lookback', 'z_entry_short', 'z_entry_long', 'z_exit', 'z_stop',
              'slope_lookback', 'max_hold_days', 'max_loss_pct', 'daily_max_loss_pct',
              'regime_threshold', 'regime_reduce_threshold']:
        print(f"  {k:<30s} = {best_params[k]}")
    print(f"  {'size_tiers':<30s} = {best_params['size_tiers']}")

    if not full_result['trades'].empty:
        print(f"\nAll Trades ({len(full_result['trades'])}):")
        print(f"  {'#':>3} | {'Entry':>10} → {'Exit':>10} | {'Dir':>5} | {'Qty':>3} | "
              f"{'Entry$':>7} | {'Exit$':>7} | {'PnL':>11} | {'Days':>4} | Reason")
        print('  ' + '─' * 100)
        for idx, (_, t) in enumerate(full_result['trades'].iterrows(), 1):
            print(f"  {idx:>3} | {str(t['entry_date'])[:10]} → {str(t['exit_date'])[:10]} | "
                  f"{t['direction']:>5s} | {t['contracts']:>3.0f}x | "
                  f"{t['entry_price']:>7.2f} | {t['exit_price']:>7.2f} | "
                  f"${t['pnl']:>+10,.2f} | {t['days_held']:>4d} | {t['exit_reason']}")

    # =========================================================================
    # Step 4: Monthly breakdown (last 24 months)
    # =========================================================================
    print(f"\n{'#'*65}")
    print(f"  Monthly Performance Breakdown (Last 24 Months)")
    print(f"{'#'*65}")

    monthly_results = []
    end_idx = len(vix_df)
    for m_idx in range(24):
        end = end_idx - m_idx * 21
        start = end - 21
        if start < best_params['z_lookback'] + 50:
            break
        warmup_start = max(0, start - best_params['z_lookback'] * 3)
        window_df = vix_df.iloc[warmup_start:end].copy()
        bt_m = VIXDirectionalBacktest(best_params)
        result_m = bt_m.run(window_df, INITIAL_CAPITAL)
        m_data = result_m['metrics']
        if m_data:
            m_data['period_start'] = vix_df.index[start].strftime('%Y-%m-%d')
            m_data['period_end'] = vix_df.index[min(end - 1, len(vix_df) - 1)].strftime('%Y-%m-%d')
            monthly_results.append(m_data)

    if monthly_results:
        mr_df = pd.DataFrame(monthly_results)
        print(f"\n{'Period':<25s} {'Return%':>8s} {'Sharpe':>8s} {'MaxDD%':>8s} {'#Trades':>8s} {'Win%':>7s} {'S_PnL':>10s} {'L_PnL':>10s}")
        print('─' * 95)
        for _, row in mr_df.iterrows():
            print(f"{row['period_start']} → {row['period_end'][-5:]}  "
                  f"{row['total_return']:>+7.2f}% {row['sharpe']:>7.3f} "
                  f"{row['max_drawdown']:>+7.2f}% {row['n_trades']:>7.0f} {row['win_rate']:>6.1f}% "
                  f"${row['short_pnl']:>+9,.0f} ${row['long_pnl']:>+9,.0f}")

        print(f"\n  Months tested:       {len(mr_df)}")
        print(f"  Monthly avg return:  {mr_df['total_return'].mean():+.2f}%")
        print(f"  Months profitable:   {(mr_df['total_return'] > 0).sum()}/{len(mr_df)} "
              f"({(mr_df['total_return'] > 0).mean() * 100:.0f}%)")
        print(f"  Avg Sharpe:          {mr_df['sharpe'].mean():.3f}")
        print(f"  Worst month:         {mr_df['total_return'].min():+.2f}%")
        print(f"  Best month:          {mr_df['total_return'].max():+.2f}%")

    # =========================================================================
    # Step 5: Recommended config
    # =========================================================================
    print(f"\n{'#'*65}")
    print(f"  Recommended vix_config.py Updates")
    print(f"{'#'*65}")
    print(f"""
# Signal
Z_LOOKBACK = {best_params['z_lookback']}
Z_ENTRY_SHORT = {best_params['z_entry_short']}
Z_ENTRY_LONG = {best_params['z_entry_long']}
Z_EXIT = {best_params['z_exit']}
Z_STOP = {best_params['z_stop']}
MAX_HOLD_DAYS = {best_params['max_hold_days']}
SLOPE_LOOKBACK = {best_params['slope_lookback']}
SLOPE_CONFIRMATION = {best_params['slope_confirmation']}

# Risk
MAX_LOSS_PCT = {best_params['max_loss_pct']}
DAILY_MAX_LOSS_PCT = {best_params['daily_max_loss_pct']}
VIX_REGIME_THRESHOLD = {best_params['regime_threshold']}
VIX_REGIME_REDUCE_THRESHOLD = {best_params['regime_reduce_threshold']}

# Position sizing
MAX_CONTRACTS = 20
CAPITAL_PER_CONTRACT = 1000
POSITION_SIZE_TIERS = {best_params['size_tiers']}
""")
