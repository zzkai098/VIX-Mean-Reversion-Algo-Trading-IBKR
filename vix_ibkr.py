"""
IBKR API wrapper and utilities for the Long-Short Leveraged Rotation strategy.
contains:
- IBKR class: Connects to IB, fetches data, places orders, and monitors PnL and some account info.
- Visualizer class: Utility for printing positions and live PnL in a readable format.
"""
import math
import pandas as pd 
import numpy as np 
from ib_async import *
from vix_config import *
from datetime import datetime

class IBKR(object):    
    def __init__(self):
        self.ib = IB()
        self.account = None
        self._active_pnls = {}        
        
    def connect(self, client_id, isTWS = True) -> None:
        """ Connect to IB Gateway/TWS """
        if isTWS:
            self.ib.connect(IB_HOST, IB_PORT, client_id)
        else:
            self.ib.connect(IB_HOST, 4002, client_id)  # Gateway paper
            
        self._set_account()
                    
    def disconnect(self) -> None:
        """Disconnect from IB"""
        self.ib.disconnect()
        
    def get_available_cash(self, account: str | None = None) -> float:
        """
        Fetch available cash for a specific account (USD by default).

        Args:
            account: Optional account number. If None, use the first account found.
        Returns:
            Available cash as float.
        """
        account_summary = self.ib.accountSummary()
        
        if account:
            account_summary = [x for x in account_summary if x.account == account]
        elif not account_summary:
            return 0.0
        
        # get AvailableFunds
        available_cash = float(
            next(
                (x.value for x in account_summary if x.tag == "AvailableFunds" and x.currency == BASE_CURRENCY),
                0
            )
        )
        return available_cash
    
    def get_positions(self, account: str | None = None) -> dict:
        """
        get current position
        """
        positions = self.ib.positions()
        pos_dict = {}
        
        for position in positions:
        # Filter by account if one is provided
            if account and position.account != account:
                continue
            
            qty = position.position
            symbol = position.contract.localSymbol
            avgcost = position.avgCost
            pos_dict[symbol] = {'qty': qty, 'avg_cost': avgcost}
        return pos_dict
    
    def get_contract_stock(self, symbol: str) -> Stock:
        """
        Create a Stock contract and qualify it asynchronously.

        Args:
            symbol: Ticker symbol, e.g., 'NFLX'

        Returns:
            Stock contract object, qualified with IB
        """
        contract = Stock(symbol, EXCHANGE, BASE_CURRENCY)
        self.ib.qualifyContracts(contract)
        return contract
        
    def get_historical_prices(self, contract: Stock, duration: str = '3 M', bar_size: str = '1 week') -> pd.DataFrame:
        """
        Fetch historical prices
        Returns a DataFrame indexed by date, columns=['open','high','low','close','volume']
        """
        # Request async historical bars
        self.ib.reqMarketDataType(3) # get delay data 
        bars = self.ib.reqHistoricalData(
                contract = contract, 
                endDateTime = '',
                durationStr = duration,
                barSizeSetting = bar_size, 
                whatToShow = 'TRADES',
                useRTH = True
            )
        
        df_bars = util.df(bars)
        df_bars = df_bars.set_index('date')
        df_bars.index = pd.to_datetime(df_bars.index).tz_localize(None)  # remove timezone
        
        return df_bars[['open','high','low','close','volume']]
    
    def get_current_price(self, contract: Stock) -> float:
        """
        Fetch the current market price for a given contract.
        Returns the last price as a float.
        """
        self.ib.reqMarketDataType(3) # get delay data 
        ticker = self.ib.reqMktData(contract, "", False, False)
        
        # Wait for the ticker to update with a last price (timeout 10s)
        max_wait = 100  # 100 × 0.1s = 10 seconds
        for _ in range(max_wait):
            if not math.isnan(ticker.last):
                return ticker.last
            # Fallback: use mid-price if bid/ask available
            if not math.isnan(ticker.bid) and not math.isnan(ticker.ask) and ticker.bid > 0:
                return (ticker.bid + ticker.ask) / 2
            util.sleep(0.1)

        print(f"[WARN] get_current_price timed out for {contract.localSymbol}")
        return float('nan')
    
    def get_pnl(self, account: str):
        """
        Monitors Profit & Loss (PnL) for a specific account.
        
        Args:
            account (str): Account ID (e.g., 'DUP173994').
            duration (int): How long to monitor in seconds.
        """
        account = account or self.account

        self.ib.cancelPnL(account)
        pnl = self.ib.reqPnL(account)
        
        return pnl
    
    def get_net_liquidation(self, account: str | None = None) -> float:
        """Fetch total net liquidation value for the account."""
        account_summary = self.ib.accountSummary()
        
        if account:
            account_summary = [x for x in account_summary if x.account == account]
        elif not account_summary:
            return 0.0
            
        net_liq = float(
            next((x.value for x in account_summary if x.tag == "NetLiquidation"), 0.0)
        )
        return net_liq

    def place_market_order(self, symbol: str, action: str, qty: int) -> None:
        """
        Abstracts the contract creation and order placement.
        action: 'BUY' or 'SELL'
        """
        if qty <= 0:
            return

        contract = self.get_contract_stock(symbol)
        order = MarketOrder(action, qty)
        self.ib.placeOrder(contract, order)

    def place_order(self, contract, action: str, qty: int) -> None:
        """
        Place a market order on any pre-qualified contract (futures, options, etc).
        action: 'BUY' or 'SELL'
        """
        if qty <= 0:
            return
        order = MarketOrder(action, qty)
        self.ib.placeOrder(contract, order)
                                    
    def _set_account(self) -> None:
        accounts = self.ib.managedAccounts()
        if accounts:
            self.account = accounts[0] 
        else:
            self.account = None
 

class Visualizer(object):    
    def __init__(self, float_format: str = "{:.2f}"):
        self.float_format = float_format
    
    def monitor_positions(self, pos_dict: dict, tickers: list | str | None = None) -> None:
        """ Prints the position dictionary, optionally filtered by tickers """
        print("Current Positions:")
        
        if not pos_dict:
            print("  No open positions.")
            return

        if isinstance(tickers, str):
            tickers = [tickers]

        found_any = False
        for symbol, data in pos_dict.items():
            # Extract just the base symbol (e.g., AAPL) if you are using localSymbol keys
            base_symbol = symbol.split()[0] 
            
            if tickers is None or base_symbol in tickers:
                print(f"{symbol:>6}: {data['qty']:>5}   @   {data['avg_cost']:<8.2f}")
                found_any = True
                
        if tickers is not None and not found_any:
            print(f"  No positions found for: {tickers}")
            
    def monitor_pnl(self, pnl, duration: int = 30):
        """ Monitors a live PnL object for a set duration. """
        if pnl is None:
            return
        
        print(f"--- Starting PnL Monitor for {pnl.account} ---")
        util.sleep(1)
        
        # fmt
        def color_pnl(val):
            if val != val: return "-" # Handle NaN
            if val > 0: return f"\033[92m+{val:,.2f}\033[0m" # Green
            if val < 0: return f"\033[91m{val:,.2f}\033[0m"  # Red
            return f"{val:.2f}"

        # PRINT HEADER
        header = (
            f"{'TIME':<10} | "
            f"{'DAILY PnL':>15} | "
            f"{'UNREALIZED':>15} | "
            f"{'REALIZED':>10}"
        )
        print("-" * 65)
        print(header)
        print("-" * 65)
                    
        count = 0
        try:
            while duration == 0 or count < duration:
                util.sleep(1)
                now = datetime.now().strftime("%H:%M:%S")
                    
                print(
                    f"{now:<10} | "
                    f"{color_pnl(pnl.dailyPnL):>24} | " 
                    f"{color_pnl(pnl.unrealizedPnL):>24} | "
                    f"{color_pnl(pnl.realizedPnL):>18}"
                    )
                count += 1
                    
        except KeyboardInterrupt:
            print("\nMonitor stopped by user.")
          
if __name__ == "__main__":
    ib = IBKR()
    ib.connect(np.random.randint(10000, 999999), isTWS=True)
    print("Available Cash:", ib.get_available_cash())
    print("Positions:", ib.get_positions())
    print("Net Liquidation:", ib.get_net_liquidation(), '\n')
    print("Historical Prices for AAPL:\n", ib.get_historical_prices(ib.get_contract_stock("AAPL")).tail())
    print("Current Price for AAPL:", ib.get_current_price(ib.get_contract_stock("AAPL")))
    
    #pnl
    pnl = ib.get_pnl(ib.account)
    visualizer = Visualizer()
    visualizer.monitor_pnl(pnl, duration=10)
    
    ib.disconnect()
    