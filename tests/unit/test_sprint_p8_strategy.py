import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone

from research.backtest.simulator import PropBacktestSimulator
from src.strategy.base import SignalAction
from src.strategy.trend_v1 import EURUSDMultiTimeframeTrendStrategy


def generate_series(trend_type: str = "bullish", count: int = 120, start_px: float = 1.0800, seed: int = 20260920) -> pd.DataFrame:
    """
    Deret harga deterministik.

    PENTING: RNG di-seed eksplisit. Sebelumnya fungsi ini memakai
    np.random.uniform tanpa seed, sehingga test bisa gagal secara acak
    (assert 1.999999999999633 >= 2.0) -- sekitar 15% run gagal.
    """
    rng = np.random.default_rng(seed)
    base_time = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)
    timestamps = [base_time + timedelta(minutes=15 * i) for i in range(count)]

    prices = [start_px]
    for i in range(1, count):
        if trend_type == "bullish":
            delta = 0.0003 + rng.uniform(-0.0001, 0.0002)
        elif trend_type == "bearish":
            delta = -0.0003 + rng.uniform(-0.0002, 0.0001)
        else:
            delta = rng.uniform(-0.0002, 0.0002)
        prices.append(prices[-1] + delta)

    data = []
    for p in prices:
        data.append({
            "open": p - 0.0001,
            "high": p + 0.0004,
            "low": p - 0.0004,
            "close": p,
            "volume": 100.0
        })

    return pd.DataFrame(data, index=timestamps)


def test_alpha_v1_bullish_signal():
    strategy = EURUSDMultiTimeframeTrendStrategy()
    df = generate_series("bullish", count=100)

    signal = strategy.generate_signal("EURUSD", df)
    assert signal.action == SignalAction.BUY
    assert signal.stop_loss < signal.entry_price
    assert signal.take_profit > signal.entry_price
    assert signal.risk_reward_ratio >= 2.0


def test_alpha_v1_bearish_signal():
    strategy = EURUSDMultiTimeframeTrendStrategy()
    df = generate_series("bearish", count=100)

    signal = strategy.generate_signal("EURUSD", df)
    assert signal.action == SignalAction.SELL
    assert signal.stop_loss > signal.entry_price
    assert signal.take_profit < signal.entry_price
    assert signal.risk_reward_ratio >= 2.0


def test_alpha_v1_backtest_integration():
    strategy = EURUSDMultiTimeframeTrendStrategy()
    df = generate_series("bullish", count=150)

    sim = PropBacktestSimulator(initial_balance=100_000.0, risk_per_trade_pct=0.5)
    res = sim.run(strategy=strategy, df=df, symbol="EURUSD")

    assert res.passed_prop_rules is True
    assert len(res.trades) > 0
    assert res.trades[0].action == "BUY"
    assert res.trades[0].lot_size > 0
