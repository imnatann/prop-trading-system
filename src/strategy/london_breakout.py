"""
Institutional London Trend Breakout Strategy.

Menerapkan arsitektur pembukaan sesi London dengan filter tren institusional:
1. Asian Session Range: Menghitung High dan Low pada sesi Asia (00:00 - 06:00 UTC).
2. Asian Range Quality: Rentang harus optimal (10 - 40 pips) agar tidak masuk saat pasar terlalu volatil atau mati.
3. Macro Trend Alignment: Hanya mengambil BUY jika harga > EMA200 dan EMA50 > EMA200; hanya SELL jika sebaliknya.
4. Execution Window: Breakout dipantau pada jam pembukaan sesi London (07:00 - 12:00 UTC).
5. Disciplined Risk: Maksimal 1 trade per hari (anti-overtrading), SL di median range / 1.5 ATR, TP minimal 1:2.0 RR.
"""
from __future__ import annotations

from typing import Dict, Optional
import numpy as np
import pandas as pd

from src.strategy.base import BaseStrategy, SignalAction, TradeSignal


class LondonTrendBreakoutStrategy(BaseStrategy):
    """Institutional London Session Trend Breakout Strategy for Forex Majors."""

    def __init__(
        self,
        min_range_pips: float = 10.0,
        max_range_pips: float = 40.0,
        min_risk_reward: float = 2.0,
        atr_multiplier: float = 1.5,
        ema_fast: int = 50,
        ema_slow: int = 200,
        atr_period: int = 14,
    ):
        super().__init__(name="London_Trend_Breakout_v1")
        self.min_range_pips = min_range_pips
        self.max_range_pips = max_range_pips
        self.min_risk_reward = min_risk_reward
        self.atr_multiplier = atr_multiplier
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.atr_period = atr_period
        self.last_traded_day: Dict[str, object] = {}

    def generate_signal(self, symbol: str, ohlcv_df: pd.DataFrame) -> TradeSignal:
        sym = symbol.upper()
        is_crypto = "BTC" in sym or "CRYPTO" in sym
        pip = 0.01 if ("JPY" in sym or is_crypto) else 0.0001
        decimals = 2 if is_crypto else (3 if "JPY" in sym else 5)

        min_bars = self.ema_slow + 5
        if len(ohlcv_df) < min_bars:
            return TradeSignal(
                symbol=symbol,
                action=SignalAction.HOLD,
                entry_price=0.0,
                stop_loss=0.0,
                take_profit=0.0,
                rationale="Insufficient bars (%d < %d)" % (len(ohlcv_df), min_bars),
            )

        curr = ohlcv_df.iloc[-1]
        prev = ohlcv_df.iloc[-2]
        curr_close = round(float(curr["close"]), decimals)
        prev_close = round(float(prev["close"]), decimals)
        curr_time = ohlcv_df.index[-1]
        curr_day = curr_time.date() if hasattr(curr_time, "date") else curr_time
        curr_hour = curr_time.hour if hasattr(curr_time, "hour") else 12

        # 1. Enforce max 1 trade per day
        if self.last_traded_day.get(sym) == curr_day:
            return TradeSignal(symbol, SignalAction.HOLD, curr_close, 0.0, 0.0, "Already traded today")

        # 2. London Morning Window (07:00 to 12:00 UTC)
        if hasattr(curr_time, "hour") and not (7 <= curr_hour <= 12):
            return TradeSignal(symbol, SignalAction.HOLD, curr_close, 0.0, 0.0, "Outside London window (07:00-12:00 UTC)")

        # 3. Asian Session Range Detection (00:00 to 06:00 UTC)
        if hasattr(curr_time, "date") and hasattr(curr_time, "hour"):
            today_bars = ohlcv_df[ohlcv_df.index.date == curr_day]
            asian_bars = today_bars[(today_bars.index.hour >= 0) & (today_bars.index.hour <= 6)]
            if len(asian_bars) < 4:
                return TradeSignal(symbol, SignalAction.HOLD, curr_close, 0.0, 0.0, "Incomplete Asian session bars")
            asian_high = float(asian_bars["high"].max())
            asian_low = float(asian_bars["low"].min())
        else:
            # Fallback for synthetic series without full day range
            asian_bars = ohlcv_df.iloc[-20:-5]
            asian_high = float(asian_bars["high"].max())
            asian_low = float(asian_bars["low"].min())

        asian_range = asian_high - asian_low
        min_range = self.min_range_pips * pip
        max_range = self.max_range_pips * pip
        if not (min_range <= asian_range <= max_range):
            return TradeSignal(symbol, SignalAction.HOLD, curr_close, 0.0, 0.0, "Asian range outside optimal bounds")

        # 4. Trend Baseline and ATR
        lookback = min(len(ohlcv_df), 220)
        c = ohlcv_df["close"].iloc[-lookback:]
        ema_fast = c.ewm(span=self.ema_fast, adjust=False).mean().iloc[-1]
        ema_slow = c.ewm(span=self.ema_slow, adjust=False).mean().iloc[-1]

        tail15 = ohlcv_df.iloc[-15:]
        tr = pd.concat([
            tail15["high"] - tail15["low"],
            (tail15["high"] - tail15["close"].shift()).abs(),
            (tail15["low"] - tail15["close"].shift()).abs(),
        ], axis=1).max(axis=1)
        atr_val = tr.iloc[-self.atr_period:].mean()
        atr = round(atr_val, decimals) if not pd.isna(atr_val) and atr_val > 0 else (15.0 * pip)
        asian_mid = (asian_high + asian_low) / 2.0

        # 5. Bullish Breakout Trigger
        if curr_close > asian_high and prev_close <= asian_high and curr_close > ema_slow and ema_fast > ema_slow:
            sl = round(max(asian_mid, curr_close - (self.atr_multiplier * atr)), decimals)
            sl_dist = round(curr_close - sl, decimals)
            if sl_dist > 5 * pip:
                tp = round(curr_close + (sl_dist * self.min_risk_reward), decimals)
                self.last_traded_day[sym] = curr_day
                return TradeSignal(
                    symbol=symbol,
                    action=SignalAction.BUY,
                    entry_price=curr_close,
                    stop_loss=sl,
                    take_profit=tp,
                    rationale="London Trend Bullish Breakout: Close > AsianHigh (%.5f), Trend Up" % asian_high,
                )

        # 6. Bearish Breakdown Trigger
        elif curr_close < asian_low and prev_close >= asian_low and curr_close < ema_slow and ema_fast < ema_slow:
            sl = round(min(asian_mid, curr_close + (self.atr_multiplier * atr)), decimals)
            sl_dist = round(sl - curr_close, decimals)
            if sl_dist > 5 * pip:
                tp = round(curr_close - (sl_dist * self.min_risk_reward), decimals)
                self.last_traded_day[sym] = curr_day
                return TradeSignal(
                    symbol=symbol,
                    action=SignalAction.SELL,
                    entry_price=curr_close,
                    stop_loss=sl,
                    take_profit=tp,
                    rationale="London Trend Bearish Breakdown: Close < AsianLow (%.5f), Trend Down" % asian_low,
                )

        return TradeSignal(symbol, SignalAction.HOLD, curr_close, 0.0, 0.0, "Inside Asian range or trend unaligned")
