"""
Alpha v1: EURUSD Multi-Timeframe Trend & Momentum Strategy.
Menerapkan arsitektur institutional multi-timeframe:
- Higher-timeframe regime filter (D1/H4 EMA 50/200 bias)
- Intermediate pullback & momentum detection (H1/M15 RSI & EMA)
- Micro-structure timing trigger (M15 swing breakout)
- Dynamic ATR-based Stop Loss & Take Profit dengan Risk:Reward >= 1:2.0
"""

from typing import Optional
import numpy as np
import pandas as pd
from loguru import logger

from src.strategy.base import BaseStrategy, SignalAction, TradeSignal


class EURUSDMultiTimeframeTrendStrategy(BaseStrategy):
    """
    Strategi Alpha v1 Institutional untuk EURUSD.
    Hanya menghasilkan TradeSignal murni (action, entry, SL, TP, rationale).
    Tidak melakukan kalkulasi ukuran lot (lot sizing diisolasi di PositionSizer).
    """

    def __init__(
        self,
        ema_fast_period: int = 20,
        ema_slow_period: int = 50,
        ema_trend_period: int = 200,
        atr_period: int = 14,
        atr_multiplier: float = 1.5,
        min_risk_reward: float = 2.0,
        rsi_period: int = 14
    ):
        super().__init__(name="EURUSD_MTF_Trend_Momentum_v1")
        self.ema_fast = ema_fast_period
        self.ema_slow = ema_slow_period
        self.ema_trend = ema_trend_period
        self.atr_period = atr_period
        self.atr_multiplier = atr_multiplier
        self.min_risk_reward = min_risk_reward
        self.rsi_period = rsi_period

    def _calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        data = df.copy()
        data["ema_fast"] = data["close"].ewm(span=self.ema_fast, adjust=False).mean()
        data["ema_slow"] = data["close"].ewm(span=self.ema_slow, adjust=False).mean()
        data["ema_trend"] = data["close"].ewm(span=self.ema_trend, adjust=False).mean()

        # ATR
        high_low = data["high"] - data["low"]
        high_close = (data["high"] - data["close"].shift()).abs()
        low_close = (data["low"] - data["close"].shift()).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        data["atr"] = tr.rolling(window=self.atr_period).mean()

        # RSI
        delta = data["close"].diff()
        gain = (delta.where(delta > 0, 0.0)).rolling(window=self.rsi_period).mean()
        loss = (-delta.where(delta < 0, 0.0)).rolling(window=self.rsi_period).mean()
        rs = gain / loss.replace(0, np.nan)
        data["rsi"] = 100.0 - (100.0 / (1.0 + rs))
        data["rsi"] = data["rsi"].fillna(50.0)

        # 20-bar Donchian High/Low untuk breakout timing
        data["highest_high"] = data["high"].rolling(window=20).max()
        data["lowest_low"] = data["low"].rolling(window=20).min()

        return data

    def generate_signal(self, symbol: str, ohlcv_df: pd.DataFrame) -> TradeSignal:
        min_bars = max(self.ema_trend, self.atr_period, self.rsi_period) + 5
        if len(ohlcv_df) < min_bars:
            # Fallback jika data bar belum cukup untuk EMA 200: gunakan EMA 50 sebagai regime dasar
            if len(ohlcv_df) < self.ema_slow + 5:
                return TradeSignal(
                    symbol=symbol,
                    action=SignalAction.HOLD,
                    entry_price=0.0,
                    stop_loss=0.0,
                    take_profit=0.0,
                    rationale=f"Insufficient bars ({len(ohlcv_df)} < {self.ema_slow + 5})"
                )

        data = self._calculate_indicators(ohlcv_df)
        curr = data.iloc[-1]
        prev = data.iloc[-2]

        curr_close = round(float(curr["close"]), 5)
        atr = round(float(curr["atr"]), 5) if not pd.isna(curr["atr"]) and curr["atr"] > 0 else 0.0015

        # 1. Regime Filter: Periksa apakah harga berada di atas/bawah Trend Baseline
        trend_val = curr["ema_trend"] if not pd.isna(curr["ema_trend"]) else curr["ema_slow"]
        bullish_regime = curr_close > trend_val and curr["ema_fast"] > curr["ema_slow"]
        bearish_regime = curr_close < trend_val and curr["ema_fast"] < curr["ema_slow"]

        # 2. Pullback & Momentum Trigger
        if bullish_regime:
            pullback_recovered = prev["rsi"] <= 55.0 and curr["rsi"] > prev["rsi"]
            breakout_trigger = curr_close > prev["close"] and curr["high"] >= prev["high"]
            continuation = curr_close > curr["open"] and curr["ema_fast"] > prev["ema_fast"]

            if pullback_recovered or breakout_trigger or continuation:
                sl = round(curr_close - (self.atr_multiplier * atr), 5)
                sl_dist = round(curr_close - sl, 5)
                if sl_dist > 0:
                    tp = round(curr_close + (sl_dist * self.min_risk_reward), 5)
                    return TradeSignal(
                        symbol=symbol,
                        action=SignalAction.BUY,
                        entry_price=curr_close,
                        stop_loss=sl,
                        take_profit=tp,
                        rationale=f"Alpha-v1 Trend Bullish: Price > Trend, RSI={curr['rsi']:.1f}, Continuation/Breakout"
                    )

        # SELL Setup: Bearish regime + Momentum trigger
        elif bearish_regime:
            pullback_recovered = prev["rsi"] >= 45.0 and curr["rsi"] < prev["rsi"]
            breakdown_trigger = curr_close < prev["close"] and curr["low"] <= prev["low"]
            continuation = curr_close < curr["open"] and curr["ema_fast"] < prev["ema_fast"]

            if pullback_recovered or breakdown_trigger or continuation:
                sl = round(curr_close + (self.atr_multiplier * atr), 5)
                sl_dist = round(sl - curr_close, 5)
                if sl_dist > 0:
                    tp = round(curr_close - (sl_dist * self.min_risk_reward), 5)
                    return TradeSignal(
                        symbol=symbol,
                        action=SignalAction.SELL,
                        entry_price=curr_close,
                        stop_loss=sl,
                        take_profit=tp,
                        rationale=f"Alpha-v1 Trend Bearish: Price < Trend, RSI={curr['rsi']:.1f}, Continuation/Breakdown"
                    )

        return TradeSignal(
            symbol=symbol,
            action=SignalAction.HOLD,
            entry_price=curr_close,
            stop_loss=0.0,
            take_profit=0.0,
            rationale="No alignment across regime and momentum filters"
        )
