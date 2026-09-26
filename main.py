"""
Forex Prop Trading System - Main Orchestrator.
Mengintegrasikan Data Ingestion, Signal Strategy, Risk Gatekeeper, dan Execution Engine.
"""

import sys
import argparse
from datetime import datetime, timezone
import pandas as pd
import numpy as np
from loguru import logger

from config.settings import settings
from src.data.news_calendar import NewsCalendar
from src.strategy.sample_strategy import MovingAverageCrossoverStrategy
from src.risk.drawdown_monitor import DrawdownMonitor
from src.risk.gatekeeper import RiskGatekeeper
from src.execution.broker_adapter import MockBrokerAdapter, MT5BrokerAdapter
from src.execution.order_manager import OrderManager
from src.safety.kill_switch import EmergencyKillSwitch


def setup_logger(log_level: str = "INFO"):
    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level=log_level
    )
    logger.add("logs/bot.log", rotation="10 MB", retention="10 days", level="DEBUG")


def generate_sample_market_data(num_bars: int = 50, start_price: float = 1.0850) -> pd.DataFrame:
    """Menghasilkan bar data sintetis untuk demonstrasi alur."""
    np.random.seed(42)
    prices = [start_price]
    for _ in range(num_bars - 1):
        change = np.random.normal(0.0001, 0.0003)
        prices.append(prices[-1] + change)

    df = pd.DataFrame({
        "open": prices,
        "high": [p + 0.0004 for p in prices],
        "low": [p - 0.0004 for p in prices],
        "close": [p + 0.0001 for p in prices],
        "volume": [1000] * num_bars
    })
    return df


def main():
    parser = argparse.ArgumentParser(description="Forex Prop Trading Engine")
    parser.add_argument("--mode", type=str, default=settings.app_mode, choices=["DEV", "LIVE"], help="Run mode: DEV or LIVE")
    args = parser.parse_args()

    setup_logger(settings.log_level)
    logger.info(f"=== Starting Forex Prop Trading System [Mode: {args.mode}] ===")

    # 1. Inisialisasi Broker Adapter
    if args.mode == "LIVE":
        logger.info("Initializing LIVE MT5 Broker Adapter...")
        broker = MT5BrokerAdapter(
            login=settings.mt5_login,
            password=settings.mt5_password,
            server=settings.mt5_server,
            path=settings.mt5_path
        )
    else:
        logger.info("Initializing DEV Mock Broker Adapter (macOS Development)...")
        broker = MockBrokerAdapter(initial_balance=100_000.0)

    if not broker.connect():
        logger.error("Could not connect to broker. Exiting.")
        return

    # 2. Inisialisasi Risk & Drawdown Monitor
    snapshot = broker.get_account_snapshot()
    drawdown_monitor = DrawdownMonitor(
        initial_balance=snapshot.balance,
        max_daily_loss_pct=settings.rules.max_daily_loss_pct,
        max_total_loss_pct=settings.rules.max_total_loss_pct,
        kill_switch_pct=settings.rules.kill_switch_daily_pct
    )

    # 3. Inisialisasi Out-of-band Emergency Kill Switch
    kill_switch = EmergencyKillSwitch(
        broker=broker,
        rules=settings.rules,
        drawdown_monitor=drawdown_monitor
    )

    if not kill_switch.monitor_and_enforce():
        logger.critical("System is LOCKED. Aborting trading execution.")
        return

    # 4. Inisialisasi Data Ingestion & News Calendar
    news_calendar = NewsCalendar(high_impact_only=True)
    economic_events = news_calendar.fetch_calendar()

    # 5. Inisialisasi Strategy Engine
    strategy = MovingAverageCrossoverStrategy(fast_period=9, slow_period=21, atr_period=14)

    # 6. Inisialisasi Risk Gatekeeper & Order Manager
    gatekeeper = RiskGatekeeper(rules=settings.rules, drawdown_monitor=drawdown_monitor)
    order_manager = OrderManager(broker=broker)

    # --- SIMULASI 1 SIKLUS PIPELINE ---
    symbol = "EURUSD"
    logger.info(f"Analyzing {symbol} for potential trade signals...")

    # Ambil data OHLCV & Harga saat ini
    ohlcv_df = generate_sample_market_data(num_bars=60)
    bid, ask, spread_pips = broker.get_symbol_price(symbol)
    open_positions = broker.get_open_positions_count()

    # PILAR 1: Formulasi & Generator Sinyal
    signal = strategy.generate_signal(symbol, ohlcv_df)
    logger.info(f"Signal Generated: {signal.action.value} | Price: {signal.entry_price:.5f} | Reason: {signal.rationale}")

    # PILAR 2: Evaluasi Risiko (Pre-Trade Gatekeeper)
    decision = gatekeeper.evaluate(
        signal=signal,
        account=snapshot,
        open_positions_count=open_positions,
        current_spread_pips=spread_pips,
        economic_events=economic_events
    )

    # PILAR 3: Eksekusi Order
    if decision.is_approved:
        order_result = order_manager.dispatch_trade(signal, decision.lot_size)
        logger.info(f"Execution Completed: Status={order_result.success}, OrderID={order_result.order_id}")
    else:
        logger.info(f"Order Rejected by Risk Engine: {decision.rejection_reason}")

    # Cek akhir keselamatan akun
    kill_switch.monitor_and_enforce()
    logger.info("=== Pipeline Cycle Finished Successfully ===")


if __name__ == "__main__":
    main()
