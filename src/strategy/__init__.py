from .base import BaseStrategy, TradeSignal, SignalAction
from .sample_strategy import MovingAverageCrossoverStrategy
from .trend_v1 import EURUSDMultiTimeframeTrendStrategy

__all__ = [
    "BaseStrategy",
    "TradeSignal",
    "SignalAction",
    "MovingAverageCrossoverStrategy",
    "EURUSDMultiTimeframeTrendStrategy",
]
