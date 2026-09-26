"""
Market Data Domain Models.
Skema Quote (Tick) dan Bar (Candlestick) standar untuk pipeline riset dan live execution.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class Quote:
    """Representasi normalized tick / quote harga."""
    symbol: str
    bid: float
    ask: float
    spread_pips: float
    timestamp_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    server_time: Optional[datetime] = None

    @property
    def mid_price(self) -> float:
        return (self.bid + self.ask) / 2.0


@dataclass
class Bar:
    """Representasi candlestick OHLCV multi-timeframe."""
    symbol: str
    timeframe: str                      # M1, M5, M15, H1, H4, D1
    timestamp_utc: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open

    @property
    def range(self) -> float:
        return self.high - self.low
