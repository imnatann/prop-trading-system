"""Genuine multi-timeframe plumbing with a hard no-look-ahead guarantee.

The trap this module exists to prevent:

  At 12:15 on M15, the H4 candle covering 12:00-16:00 is STILL FORMING. Its
  high/low/close are unknown and must never be used. Naively resampling and then
  joining H4 onto M15 leaks the future.

Rule implemented here: an HTF signal may use only the LAST FULLY CLOSED HTF bar,
forward-filled onto the lower timeframe and advanced exactly when the next HTF
bar closes.
"""
from __future__ import annotations

from typing import Dict

import numpy as np
import pandas as pd


PIP = 0.0001


def resample_ohlc(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Standard OHLC resampling. NOTE: the last bar is usually INCOMPLETE."""
    out = df.resample(rule).agg({"open": "first", "high": "max",
                                 "low": "min", "close": "last"})
    return out.dropna(how="all")


def htf_closed_only(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample to HTF, then SHIFT by one HTF bar so only closed bars remain.

    Shifting by one period means the value available at time t is the HTF bar that
    finished strictly before t. The final (possibly partial) bar is dropped because
    after the shift it would carry information not yet known.
    """
    r = resample_ohlc(df, rule)
    return r.shift(1)


def align_htf_to_ltf(ltf_index: pd.DatetimeIndex, htf: pd.DataFrame) -> pd.DataFrame:
    """Forward-fill CLOSED HTF values onto the lower-timeframe index.

    reindex(..., method="ffill") places each HTF row at its own timestamp and carries
    it forward. Because `htf` has already been shifted by one bar, the carried value
    is always a bar that has fully closed.
    """
    return htf.reindex(ltf_index, method="ffill")


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"] - df["close"].shift()).abs()
    return pd.concat([hl, hc, lc], axis=1).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return true_range(df).rolling(period).mean()


def build_mtf_frame(m15: pd.DataFrame, h1_rule: str = "1h",
                    h4_rule: str = "4h") -> pd.DataFrame:
    """Attach CLOSED H1 and H4 context onto the M15 frame.

    Returns a frame indexed like `m15` with added columns:
      h1_close, h1_ema_fast, h1_ema_slow, h1_atr
      h4_close, h4_ema_fast, h4_ema_slow
    Every value is derived from bars that had ALREADY CLOSED at that timestamp.
    """
    out = m15.copy()

    h1 = htf_closed_only(m15, h1_rule)
    h1["ema_fast"] = ema(h1["close"], 50)
    h1["ema_slow"] = ema(h1["close"], 200)
    h1["atr"] = atr(h1, 14)
    h1a = align_htf_to_ltf(m15.index, h1)
    out["h1_close"] = h1a["close"]
    out["h1_ema_fast"] = h1a["ema_fast"]
    out["h1_ema_slow"] = h1a["ema_slow"]
    out["h1_atr"] = h1a["atr"]

    h4 = htf_closed_only(m15, h4_rule)
    h4["ema_fast"] = ema(h4["close"], 50)
    h4["ema_slow"] = ema(h4["close"], 200)
    h4a = align_htf_to_ltf(m15.index, h4)
    out["h4_close"] = h4a["close"]
    out["h4_ema_fast"] = h4a["ema_fast"]
    out["h4_ema_slow"] = h4a["ema_slow"]

    return out


def build_naive_mtf_frame(m15: pd.DataFrame, h1_rule: str = "1h",
                          h4_rule: str = "4h") -> pd.DataFrame:
    """DELIBERATELY LEAKY version, kept so a test can prove the leak is real.

    No shift: the still-forming HTF bar is forward-filled, so its high/low/close
    (which are not yet known) become visible early. Used only as a negative control.
    """
    out = m15.copy()
    for rule, pre in ((h1_rule, "h1"), (h4_rule, "h4")):
        h = resample_ohlc(m15, rule)
        h["ema_fast"] = ema(h["close"], 50)
        h["ema_slow"] = ema(h["close"], 200)
        ha = h.reindex(m15.index, method="ffill")
        out[pre + "_close"] = ha["close"]
        out[pre + "_ema_fast"] = ha["ema_fast"]
        out[pre + "_ema_slow"] = ha["ema_slow"]
    return out
