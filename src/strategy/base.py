"""
Strategy & Alpha Generation Base Models.
Mendefinisikan tipe sinyal trading standar dan interface BaseStrategy.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
import pandas as pd


class SignalAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class TradeSignal:
    """Representasi sinyal trading yang dihasilkan oleh modul strategi."""
    symbol: str
    action: SignalAction
    entry_price: float
    stop_loss: float
    take_profit: float
    rationale: str
    timestamp_utc: datetime = None

    def __post_init__(self):
        if self.timestamp_utc is None:
            self.timestamp_utc = datetime.now(timezone.utc)

    @property
    def stop_loss_distance(self) -> float:
        """Jarak absolut entry ke SL."""
        return abs(self.entry_price - self.stop_loss)

    @property
    def take_profit_distance(self) -> float:
        """Jarak absolut entry ke TP."""
        return abs(self.take_profit - self.entry_price)

    @property
    def risk_reward_ratio(self) -> float:
        """Rasio Risk:Reward."""
        sl_dist = self.stop_loss_distance
        if sl_dist == 0:
            return 0.0
        return self.take_profit_distance / sl_dist


class BaseStrategy(ABC):
    """Interface dasar untuk semua strategi / model sinyal."""

    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    def generate_signal(self, symbol: str, ohlcv_df: pd.DataFrame) -> TradeSignal:
        """
        Menganalisis data pasar (OHLCV) dan menghasilkan TradeSignal.
        Jika tidak ada setup yang valid, kembalikan TradeSignal dengan action SignalAction.HOLD.
        """
        pass
