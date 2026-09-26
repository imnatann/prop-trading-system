"""
CLI Backtesting Tool for Prop Trading Strategies.

Menjalankan simulasi backtest berbasis data historis nyata (10 tahun daily atau 2 tahun 1 jam)
lengkap dengan aturan resmi prop firm (FundingPips 5% Daily Loss, 10% Max Loss):

    python -m scripts.backtest --symbol EURUSD --timeframe 1h
    python -m scripts.backtest --symbol BTCUSD --timeframe 1d
    python -m scripts.backtest --symbol EURUSD --balance 50000 --risk-pct 0.5
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

import pandas as pd

from config.prop_rules import PropFirmRules
from research.backtest.costs import CostModel
from research.backtest.simulator import PropBacktestSimulator
from research.data.real_feed import fetch_yahoo, load_csv, save_csv, save_provenance
from scripts.fundingpips_cli import banner, field, safe_main
from src.broker.models import SymbolSpec
from src.paths import REAL_DATA_DIR
from src.strategy.trend_v1 import EURUSDMultiTimeframeTrendStrategy


def run(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Run realistic backtest on real historical data.")
    parser.add_argument("--symbol", default="EURUSD", help="Symbol to test (e.g. EURUSD, BTCUSD)")
    parser.add_argument("--timeframe", default="1h", choices=["1h", "1d"], help="Candle timeframe (1h or 1d)")
    parser.add_argument("--balance", type=float, default=50000.0, help="Initial balance (default $50,000)")
    parser.add_argument("--risk-pct", type=float, default=0.5, help="Risk per trade in percent (default 0.5)")
    parser.add_argument("--limit-bars", type=int, default=0, help="Limit number of bars (0 for all)")
    args = parser.parse_args(argv)

    symbol = args.symbol.upper()
    csv_file = REAL_DATA_DIR / ("%s_%s.csv" % (symbol, args.timeframe))

    # Fetch from Yahoo if not found locally
    if not csv_file.exists():
        print("Data %s not found locally. Fetching from Yahoo Finance..." % csv_file.name)
        range_str = "2y" if args.timeframe == "1h" else "5y"
        try:
            candles, meta = fetch_yahoo(symbol, interval=args.timeframe, range_=range_str)
            save_csv(candles, csv_file)
            save_provenance(meta, csv_file.with_suffix(".provenance.json"))
            print("Downloaded %d bars successfully to %s" % (len(candles), csv_file.name))
        except Exception as exc:
            print("Failed to fetch historical data for %s: %s" % (symbol, exc))
            return 1

    df_raw = pd.read_csv(csv_file)
    if df_raw.empty:
        print("Data in %s is empty." % csv_file.name)
        return 1

    # Format dataframe for simulator
    time_col = "timestamp" if "timestamp" in df_raw.columns else "time"
    if time_col in df_raw.columns:
        df_raw["datetime"] = pd.to_datetime(df_raw[time_col])
        df_raw = df_raw.set_index("datetime")
    elif "Date" in df_raw.columns:
        df_raw["datetime"] = pd.to_datetime(df_raw["Date"])
        df_raw = df_raw.set_index("datetime")

    if args.limit_bars > 0:
        df_raw = df_raw.iloc[-args.limit_bars:]

    is_crypto = "BTC" in symbol or "CRYPTO" in symbol
    spec = SymbolSpec(
        symbol=symbol,
        tick_size=0.01 if is_crypto else 0.00001,
        tick_value=0.01 if is_crypto else 1.0,
        contract_size=1.0 if is_crypto else 100000.0,
        volume_min=0.01,
        volume_max=1.0 if is_crypto else 50.0,
        volume_step=0.01,
        digits=2 if is_crypto else 5,
    )

    cost_model = CostModel(
        spread_pips=25.0 if is_crypto else 1.2,
        slippage_pips=5.0 if is_crypto else 0.3,
        commission_per_lot=0.0 if is_crypto else 3.0,
    )

    rules = PropFirmRules(
        max_daily_loss_pct=5.0,
        max_total_loss_pct=10.0,
    )

    simulator = PropBacktestSimulator(
        rules=rules,
        cost_model=cost_model,
        spec=spec,
        initial_balance=args.balance,
        risk_per_trade_pct=args.risk_pct,
    )

    strategy = EURUSDMultiTimeframeTrendStrategy()

    banner("Backtesting Strategy on %s (%s)" % (symbol, args.timeframe))
    field("Symbol", symbol)
    field("Strategy", strategy.name)
    field("Historical Bars", len(df_raw))
    field("Initial Balance", "$%.2f" % args.balance)
    field("Risk per trade", "%.2f%%" % args.risk_pct)
    field("Data Span", "%s to %s" % (df_raw.index[0], df_raw.index[-1]))
    print()

    print("Running event-driven simulation...")
    result = simulator.run(strategy, df_raw, symbol=symbol)

    banner("Backtest Results")
    field("Initial Balance", "$%.2f" % result.initial_balance)
    field("Final Balance", "$%.2f" % result.final_balance)
    field("Net Profit ($)", "$%.2f" % result.net_profit)
    field("Total Trades", result.total_trades)
    field("Winning Trades", result.winning_trades)
    field("Losing Trades", result.losing_trades)
    field("Win Rate", "%.1f%%" % result.win_rate)
    field("Profit Factor", "%.2f" % result.profit_factor)
    field("Max Daily Drawdown", "%.2f%% (Limit 5.0%%)" % result.max_daily_drawdown_pct)
    field("Max Total Drawdown", "%.2f%% (Limit 10.0%%)" % result.max_total_drawdown_pct)
    field("Passed Prop Firm Rules", "YES (CONGRATS!)" if result.passed_prop_rules else ("FAILED (%s)" % result.breach_reason))
    print()

    if result.trades:
        print("Last 5 Trades Sample:")
        hdr = "%-20s | %-5s | %-5s | %-8s | %-8s | %-9s | %-10s" % (
            "Time", "Action", "Lot", "Entry", "Exit", "PnL ($)", "Exit Reason"
        )
        print(hdr)
        print("-" * len(hdr))
        for t in result.trades[-5:]:
            t_time = str(t.entry_time)[:19]
            print("%-20s | %-5s | %-5.2f | %-8.5f | %-8.5f | $%-8.2f | %-10s" % (
                t_time, t.action, t.lot_size, t.entry_price, t.exit_price, t.pnl, t.exit_reason
            ))
        print()

    return 0


if __name__ == "__main__":
    sys.exit(safe_main(run))
