"""
Fetch extended VIX futures history from IBKR for backtesting.

Approach: IBKR only serves history for currently active contracts (~6 months).
To get multi-year data, we chain expired contracts month by month:
  - For each month since start_year, resolve the VX contract for that month
  - Fetch its historical close prices
  - Build continuous VX1 (front month) and VX2 (second month) series
  - Compute spread = VX2 - VX1

Output: vix_futures_history.csv with columns:
    date, vx1, vx2, vx1_volume, vx2_volume, spread, volume
"""

import time
import numpy as np
import pandas as pd
from datetime import datetime
from ib_async import *
from vix_ibkr import IBKR


def fetch_vx_contract_history(ib: IB, year: int, month: int,
                               duration: str = '60 D',
                               bar_size: str = '1 day') -> pd.DataFrame:
    """
    Fetch historical data for a specific VIX futures contract month.
    Returns DataFrame with OHLCV indexed by date.
    """
    # Format expiry as YYYYMM
    expiry = f"{year}{month:02d}"

    contract = Future(symbol='VIX', exchange='CFE', currency='USD',
                      lastTradeDateOrContractMonth=expiry,
                      tradingClass='VX')

    try:
        details = ib.reqContractDetails(contract)
        if not details:
            return pd.DataFrame()

        qualified = details[0].contract
        ib.qualifyContracts(qualified)

        # Use end date as the last trade date of this contract
        end_date = qualified.lastTradeDateOrContractMonth
        # Format for IBKR: YYYYMMDD HH:MM:SS
        end_dt = f"{end_date} 23:59:59"

        ib.reqMarketDataType(3)  # delayed data
        bars = ib.reqHistoricalData(
            contract=qualified,
            endDateTime=end_dt,
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow='TRADES',
            useRTH=True,
            timeout=30
        )

        if not bars:
            return pd.DataFrame()

        df = util.df(bars)
        df = df.set_index('date')
        df.index = pd.to_datetime(df.index).tz_localize(None)
        df['contract'] = qualified.localSymbol
        df['expiry'] = end_date

        return df[['open', 'high', 'low', 'close', 'volume', 'contract', 'expiry']]

    except Exception as e:
        print(f"  [WARN] Failed {expiry}: {e}")
        return pd.DataFrame()


def build_continuous_series(ib: IB, start_year: int = 2020,
                             end_year: int = 2026) -> pd.DataFrame:
    """
    Build continuous VX1/VX2 series by chaining monthly contracts.

    For each trading day:
      VX1 = the nearest active (not yet expired) contract
      VX2 = the next one after VX1
    """
    print(f"Fetching VIX futures contracts from {start_year} to {end_year}...")

    all_contracts = {}  # {expiry_str: DataFrame}

    for year in range(start_year, end_year + 1):
        for month in range(1, 13):
            # Skip future months
            if year == end_year and month > datetime.now().month + 2:
                break

            expiry = f"{year}{month:02d}"
            print(f"  Fetching VX {expiry}...", end=" ", flush=True)

            df = fetch_vx_contract_history(ib, year, month, duration='60 D')

            if df.empty:
                print("no data")
            else:
                all_contracts[expiry] = df
                print(f"{len(df)} bars")

            time.sleep(0.5)  # Rate limit

    if not all_contracts:
        print("[ERROR] No contract data fetched.")
        return pd.DataFrame()

    print(f"\nFetched {len(all_contracts)} contract months. Building continuous series...")

    # Collect all unique trading dates
    all_dates = set()
    for df in all_contracts.values():
        all_dates.update(df.index.tolist())
    all_dates = sorted(all_dates)

    # For each date, find VX1 (nearest not-yet-expired) and VX2 (next one)
    records = []
    sorted_expiries = sorted(all_contracts.keys())

    for date in all_dates:
        date_str = date.strftime('%Y%m%d') if hasattr(date, 'strftime') else str(date)[:10].replace('-', '')

        # Find active contracts on this date (expiry >= date)
        active = []
        for exp in sorted_expiries:
            exp_df = all_contracts[exp]
            # Contract expiry must be after this date
            if exp_df['expiry'].iloc[0] >= date_str and date in exp_df.index:
                active.append((exp, exp_df.loc[date]))

        if len(active) >= 2:
            vx1_exp, vx1_data = active[0]
            vx2_exp, vx2_data = active[1]

            records.append({
                'date': date,
                'vx1': vx1_data['close'],
                'vx2': vx2_data['close'],
                'vx1_volume': vx1_data['volume'],
                'vx2_volume': vx2_data['volume'],
                'vx1_contract': vx1_data['contract'],
                'vx2_contract': vx2_data['contract'],
            })

    result = pd.DataFrame(records)
    if result.empty:
        print("[ERROR] Could not build continuous series.")
        return pd.DataFrame()

    result = result.set_index('date').sort_index()
    result['spread'] = result['vx2'] - result['vx1']
    result['volume'] = result['vx1_volume'] + result['vx2_volume']

    # Drop duplicate dates
    result = result[~result.index.duplicated(keep='last')]

    print(f"\nContinuous series: {len(result)} days, "
          f"{result.index[0].date()} → {result.index[-1].date()}")
    print(f"Spread range: [{result['spread'].min():.4f}, {result['spread'].max():.4f}]")
    print(f"Spread mean:  {result['spread'].mean():.4f}, std: {result['spread'].std():.4f}")

    return result


if __name__ == "__main__":
    print("=" * 60)
    print("  VIX Futures History Fetcher — IBKR")
    print("=" * 60)

    ib = IBKR()
    ib.connect(np.random.randint(100000, 999999), isTWS=True)

    if not ib.account:
        print("[FATAL] Failed to connect to IBKR.")
        exit(1)

    print(f"[OK] Connected to {ib.account}\n")

    # Fetch 2020-2026 (~6 years of data)
    history = build_continuous_series(ib.ib, start_year=2020, end_year=2026)

    if not history.empty:
        out_file = "vix_futures_history.csv"
        # Save columns needed for backtest
        save_cols = ['vx1', 'vx2', 'vx1_volume', 'vx2_volume', 'spread', 'volume']
        history[save_cols].to_csv(out_file)
        print(f"\nSaved to {out_file}: {len(history)} rows")
        print(f"\nSample data (last 5 rows):")
        print(history[save_cols].tail())

    ib.disconnect()
    print("\nDone.")
