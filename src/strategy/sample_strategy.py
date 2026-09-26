"""
Sample Quantitative Strategy: EMA Crossover with ATR Volatility SL/TP.
Contoh strategi sederhana untuk memvalidasi alur pipeline secara utuh.
"""

import pandas as pd
from .base import BaseStrategy, TradeSignal, SignalAction


class MovingAverageCrossoverStrategy(BaseStrategy):
    """
    Strategi Exponential Moving Average (EMA) Crossover
    dengan Stop Loss dinamis berbasis Average True Range (ATR).
    """

    def __init__(self, fast_period: int = 9, slow_period: int = 21, atr_period: int = 14, risk_reward: float = 2.0):
        super().__init__(name="EMA_ATR_Crossover")
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.atr_period = atr_period
        self.risk_reward = risk_reward

    def _calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        data = df.copy()
        data["ema_fast"] = data["close"].ewm(span=self.fast_period, adjust=False).mean()
        data["ema_slow"] = data["close"].ewm(span=self.slow_period, adjust=False).mean()

        # ATR calculation
        high_low = data["high"] - data["low"]
        high_close = (data["high"] - data["close"].shift()).abs()
        low_close = (data["low"] - data["close"].shift()).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        data["atr"] = tr.rolling(window=self.atr_period).mean()

        return data

    def generate_signal(self, symbol: str, ohlcv_df: pd.DataFrame) -> TradeSignal:
        if len(ohlcv_df) < max(self.slow_period, self.atr_period) + 2:
            return TradeSignal(
                symbol=symbol,
                action=SignalAction.HOLD,
                entry_price=0.0,
                stop_loss=0.0,
                take_profit=0.0,
                rationale="Insufficient historical data"
            )

        data = self._calculate_indicators(ohlcv_df)
        curr = data.iloc[-1]
        prev = data.iloc[-2]

        entry_price = float(curr["close"])
        atr = float(curr["atr"]) if not pd.isna(curr["atr"]) else 0.0015

        # Bullish Crossover (Fast crosses above Slow)
        if prev["ema_fast"] <= prev["ema_slow"] and curr["ema_fast"] > curr["ema_slow"]:
            sl = round(entry_price - (1.5 * atr), 5)
            sl_distance = entry_price - sl
            tp = round(entry_price + (sl_distance * self.risk_reward), 5)
            return TradeSignal(
                symbol=symbol,
                action=SignalAction.BUY,
                entry_price=entry_price,
                stop_loss=sl,
                take_profit=tp,
                rationale=f"Bullish EMA({self.fast_period}/{self.slow_period}) crossover with ATR({self.atr_period}) SL"
            )

        # Bearish Crossover (Fast crosses below Slow)
        elif prev["ema_fast"] >= prev["ema_slow"] and curr["ema_fast"] < curr["ema_slow"]:
            sl = round(entry_price + (1.5 * atr), 5)
            sl_distance = sl - entry_price
            tp = round(entry_price - (sl_distance * self.risk_reward), 5)
            return TradeSignal(
                symbol=symbol,
                action=SignalAction.SELL,
                entry_price=entry_price,
                stop_loss=sl,
                take_profit=tp,
                rationale=f"Bearish EMA({self.fast_period}/{self.slow_period}) crossover with ATR({self.atr_period}) SL"
            )

        return TradeSignal(
            symbol=symbol,
            action=SignalAction.HOLD,
            entry_price=entry_price,
            stop_loss=0.0,
            take_profit=0.0,
            rationale="No crossover setup"
        )
