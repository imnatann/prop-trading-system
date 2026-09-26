import pytest
from datetime import datetime, timedelta, timezone
import pandas as pd

from src.data.market import Quote, Bar
from src.data.validation import FreshnessGate
from src.data.bars import BarAggregator


def test_quote_and_bar_models():
    now = datetime.now(timezone.utc)
    quote = Quote(symbol="EURUSD", bid=1.0850, ask=1.0852, spread_pips=2.0, timestamp_utc=now)
    assert abs(quote.mid_price - 1.0851) < 1e-6

    bullish_bar = Bar(symbol="EURUSD", timeframe="M15", timestamp_utc=now, open=1.0820, high=1.0860, low=1.0815, close=1.0850)
    assert bullish_bar.is_bullish is True
    assert bullish_bar.is_bearish is False
    assert abs(bullish_bar.range - 0.0045) < 1e-6


def test_freshness_gate_valid_and_stale():
    gate = FreshnessGate(max_stale_seconds=3.0, max_spread_pips=2.5)
    now = datetime.now(timezone.utc)

    # 1. Fresh Quote
    fresh = Quote(symbol="EURUSD", bid=1.0850, ask=1.0851, spread_pips=1.0, timestamp_utc=now)
    res = gate.validate_quote(fresh, now_utc=now)
    assert res.is_valid is True
    assert res.reason is None

    # 2. Stale Quote (10 seconds old)
    old_time = now - timedelta(seconds=10.0)
    stale = Quote(symbol="EURUSD", bid=1.0850, ask=1.0851, spread_pips=1.0, timestamp_utc=old_time)
    res = gate.validate_quote(stale, now_utc=now)
    assert res.is_valid is False
    assert "Stale quote rejected" in res.reason


def test_freshness_gate_anomalies_rejection():
    gate = FreshnessGate(max_stale_seconds=5.0, max_spread_pips=3.0)
    now = datetime.now(timezone.utc)

    # Inverted spread
    inverted = Quote(symbol="EURUSD", bid=1.0860, ask=1.0850, spread_pips=-1.0, timestamp_utc=now)
    res = gate.validate_quote(inverted, now_utc=now)
    assert res.is_valid is False
    assert "Inverted spread" in res.reason

    # Spread spike
    spike = Quote(symbol="EURUSD", bid=1.0850, ask=1.0856, spread_pips=6.0, timestamp_utc=now)
    res = gate.validate_quote(spike, now_utc=now)
    assert res.is_valid is False
    assert "Spread spike rejected" in res.reason


def test_bar_aggregator_resampling():
    base_time = datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)
    bars = [
        Bar("EURUSD", "M1", base_time + timedelta(minutes=0), 1.0800, 1.0810, 1.0795, 1.0805, 10.0),
        Bar("EURUSD", "M1", base_time + timedelta(minutes=1), 1.0805, 1.0820, 1.0800, 1.0815, 15.0),
        Bar("EURUSD", "M1", base_time + timedelta(minutes=2), 1.0815, 1.0825, 1.0810, 1.0820, 20.0),
        Bar("EURUSD", "M1", base_time + timedelta(minutes=3), 1.0820, 1.0830, 1.0818, 1.0825, 25.0),
        Bar("EURUSD", "M1", base_time + timedelta(minutes=4), 1.0825, 1.0835, 1.0820, 1.0830, 30.0),
    ]
    df = BarAggregator.dataframe_from_bars(bars)
    assert len(df) == 5

    resampled = BarAggregator.resample_ohlcv(df, "5min")
    assert len(resampled) == 1
    row = resampled.iloc[0]
    assert row["open"] == 1.0800
    assert row["high"] == 1.0835
    assert row["low"] == 1.0795
    assert row["close"] == 1.0830
    assert row["volume"] == 100.0
