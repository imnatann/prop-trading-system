from .base import BaseStrategy, TradeSignal, SignalAction
from .sample_strategy import MovingAverageCrossoverStrategy
from .trend_v1 import EURUSDMultiTimeframeTrendStrategy
from .london_breakout import LondonTrendBreakoutStrategy

__all__ = [
    "BaseStrategy",
    "TradeSignal",
    "SignalAction",
    "MovingAverageCrossoverStrategy",
    "EURUSDMultiTimeframeTrendStrategy",
    "LondonTrendBreakoutStrategy",
]
