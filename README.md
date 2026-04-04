# VIX Mean Reversion Algo Trading (IBKR)

A fully automated VIX futures mean-reversion trading system with live execution via Interactive Brokers API. 

## Architecture

```
vix_main.py        → Entry point: connects to IBKR, launches strategy engine + automation bot
vix_strategy.py    → Core engine (7 modular classes)
  ├─ VIXContractManager    VIX futures contract resolution (auto front-month selection)
  ├─ VIXDataEngine         Dual data source (yfinance + IBKR), local persistence
  ├─ VIXSignalEngine       Rolling z-score signals, asymmetric thresholds, regime shift detection
  ├─ VIXRiskManager        Six-layer exit system
  ├─ PositionTracker       Position state persistence, pyramid management
  ├─ VIXStrategyEngine     Orchestrates daily signal → risk → execution workflow
  └─ VIXAutomationBot      Scheduled signal checks + minute-level risk monitoring
vix_news.py        → NLP sentiment engine (GDELT + VADER geopolitical news filtering)
vix_ibkr.py        → IBKR API wrapper (connection, market data, order execution, PnL)
vix_backtest.py    → Backtest engine with rolling monthly parameter optimization
vix_config.py      → Centralized configuration (signal / risk / sizing / scheduling / IBKR)
```

## Strategy

- **Signal**: VIX spot rolling z-score with asymmetric long/short thresholds (contango/backwardation structure)
- **Filters**: VX1-VIX basis filter, dual-lookback regime shift detection (10d vs 30d z-score divergence), volume confirmation
- **NLP Enhancement**: GDELT real-time geopolitical news → VADER sentiment scoring → signal filtering
- **Risk Management**: Six-layer exit system (graduated profit-taking / z-score stop / absolute $ stop / max hold / daily loss cap / regime filter)
- **Position Sizing**: Z-score tiered sizing with pyramid add-on support

## Backtest Engine

- Rolling monthly window optimization
- 1,500+ parameter combinations via Grid Search
- Metrics: Sharpe, Sortino, Calmar, Max Drawdown, Win Rate, Profit Factor

## Tech Stack

Python, IBKR API (ib_async), pandas, NumPy, yfinance, NLTK (VADER), plotext, schedule
