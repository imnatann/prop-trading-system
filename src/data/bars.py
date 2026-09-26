"""
Bar Aggregation & Resampling Layer.
Mengubah data tick atau bar rendah (M1) menjadi timeframes lebih tinggi (M15, H1, H4, D1)
dengan menjaga integritas timestamp batas bar.
"""

from datetime import datetime, timezone
from typing import List
import pandas as pd

from src.data.market import Bar, Quote


class BarAggregator:
    """Aggregator candlestick untuk analisis multi-timeframe."""

    @staticmethod
    def dataframe_from_bars(bars: List[Bar]) -> pd.DataFrame:
        if not bars:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
        data = [{
            "timestamp": b.timestamp_utc,
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume
        } for b in bars]
        df = pd.DataFrame(data)
        df.set_index("timestamp", inplace=True)
        df.sort_index(inplace=True)
        return df

    @staticmethod
    def resample_ohlcv(df: pd.DataFrame, target_rule: str) -> pd.DataFrame:
        """
        Resample data OHLCV ke rule baru (misal '15min', '1h', '4h', '1d').
        """
        if df.empty:
            return df
        resampled = df.resample(target_rule).agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum"
        }).dropna()
        return resampled
