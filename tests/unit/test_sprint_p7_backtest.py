import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone

from config.prop_rules import PropFirmRules
from research.backtest.costs import CostModel
from research.backtest.simulator import PropBacktestSimulator
from src.strategy.base import BaseStrategy, SignalAction, TradeSignal


class DummyTrendStrategy(BaseStrategy):
    """Strategi uji sederhana: BUY jika close > MA20, SELL jika close < MA20."""

    def __init__(self):
        super().__init__(name="DummyTrend")

    def generate_signal(self, symbol: str, ohlcv_df: pd.DataFrame) -> TradeSignal:
        if len(ohlcv_df) < 20:
            return TradeSignal(symbol, SignalAction.HOLD, 0, 0, 0, "Not enough bars")

        close = ohlcv_df["close"]
        ma = close.rolling(20).mean().iloc[-1]
        curr = close.iloc[-1]

        if curr > ma + 0.0010:
            return TradeSignal(
                symbol=symbol,
                action=SignalAction.BUY,
                entry_price=curr,
                stop_loss=curr - 0.0020,  # 20 pips SL
                take_profit=curr + 0.0040, # 40 pips TP (1:2 RR)
                rationale="Above MA"
            )
        elif curr < ma - 0.0010:
            return TradeSignal(
                symbol=symbol,
                action=SignalAction.SELL,
                entry_price=curr,
                stop_loss=curr + 0.0020,
                take_profit=curr - 0.0040,
                rationale="Below MA"
            )
        return TradeSignal(symbol, SignalAction.HOLD, 0, 0, 0, "No setup")


def generate_synthetic_ohlcv(bars_count: int = 150, start_price: float = 1.0850) -> pd.DataFrame:
    base_time = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)
    timestamps = [base_time + timedelta(minutes=15 * i) for i in range(bars_count)]

    # Generate upward trend
    np.random.seed(42)
    returns = np.random.normal(0.0001, 0.0005, bars_count)
    prices = start_price + np.cumsum(returns)

    data = []
    for i, p in enumerate(prices):
        o = p
        h = p + abs(np.random.normal(0, 0.0004))
        l = p - abs(np.random.normal(0, 0.0004))
        c = (o + h + l) / 3.0
        data.append({
            "open": o,
            "high": max(h, o, c),
            "low": min(l, o, c),
            "close": c,
            "volume": 100.0
        })

    df = pd.DataFrame(data, index=timestamps)
    return df


def test_backtest_simulator_profitable_run():
    df = generate_synthetic_ohlcv(bars_count=120)
    strategy = DummyTrendStrategy()
    sim = PropBacktestSimulator(initial_balance=100_000.0, risk_per_trade_pct=0.5)

    res = sim.run(strategy=strategy, df=df, symbol="EURUSD")

    assert res.initial_balance == 100_000.0
    assert len(res.equity_curve) > 0
    assert res.passed_prop_rules is True
    assert res.max_daily_drawdown_pct < 5.0
    assert res.max_total_drawdown_pct < 10.0


def test_backtest_simulator_breach_detection():
    # Buat market anjlok parah untuk menguji deteksi breach
    base_time = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)
    timestamps = [base_time + timedelta(minutes=15 * i) for i in range(50)]

    # Strategi yang memaksa BUY dengan lot besar
    class ForcingBuyStrategy(BaseStrategy):
        def generate_signal(self, symbol: str, ohlcv_df: pd.DataFrame) -> TradeSignal:
            curr = ohlcv_df["close"].iloc[-1]
            return TradeSignal(
                symbol=symbol,
                action=SignalAction.BUY,
                entry_price=curr,
                stop_loss=curr - 0.0500,  # 500 pips SL
                take_profit=curr + 0.0500,
                rationale="Force Buy"
            )

    # Market yang crash
    data = []
    curr = 1.0850
    for i in range(50):
        if i > 25:
            curr -= 0.0030  # Jatuh 30 pip per bar
        data.append({"open": curr, "high": curr + 0.0005, "low": curr - 0.0010, "close": curr - 0.0005, "volume": 50})

    df = pd.DataFrame(data, index=timestamps)
    rules = PropFirmRules(max_daily_loss_pct=2.0)  # Ketat 2%
    sim = PropBacktestSimulator(rules=rules, initial_balance=100_000.0, risk_per_trade_pct=3.0)

    res = sim.run(strategy=ForcingBuyStrategy("Forced"), df=df, symbol="EURUSD")

    assert res.passed_prop_rules is False
    assert "Breached Max Daily Drawdown" in (res.breach_reason or "")
