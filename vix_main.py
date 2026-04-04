"""
VIX Directional Futures Strategy — Entry Point

Connects to IBKR, initializes the directional VIX strategy engine,
runs an initial daily cycle, and starts the automation bot.

Usage:
    python vix_main.py

Prerequisites:
    - IB TWS or Gateway running and logged in (paper: port 7497)
    - VIX futures market data subscription on IBKR
    - Python deps: ib_async, pandas, numpy, schedule, pytz, yfinance
"""
import nltk
import numpy as np
from vix_ibkr import IBKR
from vix_config import *
from vix_strategy import VIXStrategyEngine, VIXAutomationBot


def _ensure_nltk_data():
    """Download VADER lexicon on first run; no-op if already present."""
    try:
        nltk.data.find('sentiment/vader_lexicon')
    except LookupError:
        print("[SETUP] Downloading VADER lexicon (one-time)...")
        nltk.download('vader_lexicon', quiet=True)


def main():
    _ensure_nltk_data()
    print("=" * 60)
    print("  VIX Directional Futures — Live Trading System")
    print("=" * 60)

    # Connect to IBKR
    ib = IBKR()
    ib.connect(client_id=np.random.randint(100000, 999999), isTWS=True)

    if not ib.account:
        print("[FATAL] Failed to connect to IBKR. Ensure TWS/Gateway is running.")
        return

    print(f"[OK] Connected to IBKR account: {ib.account}")
    print(f"[OK] Net Liquidation: ${ib.get_net_liquidation():,.2f}")
    print(f"[OK] Available Cash:  ${ib.get_available_cash():,.2f}")

    # Initialize strategy engine
    engine = VIXStrategyEngine(ibkr=ib, verbose=VERBOSE)

    # Run initial daily cycle on startup
    print("\n--- Running initial daily cycle on startup ---")
    engine.run_daily_cycle()

    # Start automation bot
    bot = VIXAutomationBot(strategy_engine=engine)
    bot.start(risk_interval_minutes=RISK_INTERVAL_MINUTES)


if __name__ == "__main__":
    main()
