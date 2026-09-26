"""Alpha v2: genuine H4-regime -> H1-setup -> M15-trigger, cost-aware trend continuation.

Pre-registered in docs/RESEARCH_PROTOCOL_v2.md. The grid is FOUR configurations and
is closed. Adding a fifth after seeing results is a protocol violation.

Design rules that are NOT negotiable here:

* Each timeframe reads only bars derived from data that had already CLOSED at the
  decision timestamp (see research/mtf.py). No forming HTF bar is ever visible.
* All decisions are made on M15 bar t and executed at M15 bar t+1 open. Signal and
  fill never share a bar.
* Parameters come from the frozen grid only.
* Nothing in this module computes, prints or exposes performance. It emits signals.
  Evaluation lives elsewhere so that a library import can never leak a result.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from src.strategy.base import BaseStrategy, SignalAction, TradeSignal

PIP = 0.0001


# ---------------------------------------------------------------------------
# The frozen grid. Exactly four configurations.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AlphaV2Config:
    name: str
    pullback_atr: float      # H1 retracement depth toward EMA50, in H1 ATR units
    breakout_bars: int       # M15 breakout lookback
    atr_stop_mult: float = 1.5
    r_multiple: float = 2.0
    time_stop_bars: int = 96
    h4_fast: int = 50
    h4_slow: int = 200
    h1_fast: int = 50
    h1_slow: int = 200


FROZEN_GRID: List[AlphaV2Config] = [
    AlphaV2Config("A", pullback_atr=0.5, breakout_bars=8),
    AlphaV2Config("B", pullback_atr=0.5, breakout_bars=20),
    AlphaV2Config("C", pullback_atr=1.0, breakout_bars=8),
    AlphaV2Config("D", pullback_atr=1.0, breakout_bars=20),
]


def frozen_grid() -> List[AlphaV2Config]:
    """The complete search space. Four, permanently."""
    return list(FROZEN_GRID)


@dataclass
class RegimeState:
    """H4 regime as of the last CLOSED H4 bar."""
    long_ok: bool
    short_ok: bool
    h4_close: float
    h4_fast: float
    h4_slow: float


@dataclass
class SetupState:
    """H1 pullback state as of the last CLOSED H1 bar."""
    pulled_back_long: bool
    pulled_back_short: bool
    h1_close: float
    h1_fast: float
    h1_atr: float


def h4_regime(fast: float, slow: float, close: float) -> RegimeState:
    """Trend direction only. Deliberately minimal - low degrees of freedom."""
    if not np.isfinite([fast, slow, close]).all():
        return RegimeState(False, False, close, fast, slow)
    return RegimeState(long_ok=fast > slow, short_ok=fast < slow,
                       h4_close=close, h4_fast=fast, h4_slow=slow)


def h1_setup_long(h1_close: float, h1_fast: float, h1_atr: float,
                  pullback_atr: float) -> bool:
    """Price has retraced back toward/through the H1 fast EMA by at least
    `pullback_atr` ATR units, i.e. a pullback within an uptrend."""
    if not np.isfinite([h1_close, h1_fast, h1_atr]).all() or h1_atr <= 0:
        return False
    depth = h1_fast - h1_close
    need = pullback_atr * h1_atr
    # relative tolerance: binary floats make an exact ">= 1.0 ATR" comparison fail
    # for values that are equal in decimal (1.1050 - 1.1000 < 0.0050 exactly).
    return depth >= need - 1e-9 * max(1.0, abs(need))


def h1_setup_short(h1_close: float, h1_fast: float, h1_atr: float,
                   pullback_atr: float) -> bool:
    if not np.isfinite([h1_close, h1_fast, h1_atr]).all() or h1_atr <= 0:
        return False
    depth = h1_close - h1_fast
    need = pullback_atr * h1_atr
    return depth >= need - 1e-9 * max(1.0, abs(need))


def m15_breakout_long(highs: np.ndarray, close: float, n: int) -> bool:
    """Close breaks the highest high of the previous n bars (excluding this one)."""
    if len(highs) < n + 1:
        return False
    prior = highs[-(n + 1):-1]
    if not np.isfinite(prior).all():
        return False
    return close > float(np.max(prior))


def m15_breakout_short(lows: np.ndarray, close: float, n: int) -> bool:
    if len(lows) < n + 1:
        return False
    prior = lows[-(n + 1):-1]
    if not np.isfinite(prior).all():
        return False
    return close < float(np.min(prior))


def risk_levels(action: SignalAction, entry_ref: float, atr: float,
                cfg: AlphaV2Config) -> Optional[tuple]:
    """ATR stop and a fixed R multiple. Returns None if the stop is degenerate."""
    if not np.isfinite(atr) or atr <= 0:
        return None
    dist = cfg.atr_stop_mult * atr
    if dist <= 0:
        return None
    if action == SignalAction.BUY:
        sl = entry_ref - dist
        tp = entry_ref + dist * cfg.r_multiple
    else:
        sl = entry_ref + dist
        tp = entry_ref - dist * cfg.r_multiple
    if action == SignalAction.BUY and not (sl < entry_ref < tp):
        return None
    if action == SignalAction.SELL and not (tp < entry_ref < sl):
        return None
    return sl, tp


class AlphaV2MTFStrategy(BaseStrategy):
    """H4 regime -> H1 setup -> M15 trigger.

    The caller supplies a frame that ALREADY carries closed-bar HTF context
    (see research.mtf.build_mtf_frame). This class does no resampling itself, so it
    cannot accidentally reintroduce a look-ahead.
    """

    REQUIRED = ("close", "high", "low", "open",
                "h1_close", "h1_ema_fast", "h1_atr",
                "h4_close", "h4_ema_fast", "h4_ema_slow")

    def __init__(self, cfg: AlphaV2Config):
        super().__init__(name="AlphaV2_MTF_%s" % cfg.name)
        self.cfg = cfg

    # convenience for tests and callers
    @property
    def config(self) -> AlphaV2Config:
        return self.cfg

    def generate_signal(self, symbol: str, ohlcv_df: pd.DataFrame) -> TradeSignal:
        cfg = self.cfg
        missing = [c for c in self.REQUIRED if c not in ohlcv_df.columns]
        if missing:
            raise ValueError("AlphaV2 needs MTF columns; missing %s" % missing)
        if len(ohlcv_df) < 22:
            return self._hold(symbol, ohlcv_df, "insufficient bars")

        cur = ohlcv_df.iloc[-1]

        # ---- H4 regime (closed bar only) ----
        reg = h4_regime(float(cur["h4_ema_fast"]), float(cur["h4_ema_slow"]),
                        float(cur["h4_close"]))
        if not (reg.long_ok or reg.short_ok):
            return self._hold(symbol, ohlcv_df, "no H4 regime")

        # ---- H1 setup (closed bar only) ----
        h1_close = float(cur["h1_close"])
        h1_fast = float(cur["h1_ema_fast"])
        h1_atr = float(cur["h1_atr"])

        action: Optional[SignalAction] = None
        if reg.long_ok and h1_setup_long(h1_close, h1_fast, h1_atr, cfg.pullback_atr):
            action = SignalAction.BUY
        elif reg.short_ok and h1_setup_short(h1_close, h1_fast, h1_atr, cfg.pullback_atr):
            action = SignalAction.SELL
        if action is None:
            return self._hold(symbol, ohlcv_df, "no H1 pullback setup")

        # ---- M15 trigger ----
        highs = ohlcv_df["high"].to_numpy(dtype=float)
        lows = ohlcv_df["low"].to_numpy(dtype=float)
        close = float(cur["close"])
        if action == SignalAction.BUY and not m15_breakout_long(highs, close,
                                                               cfg.breakout_bars):
            return self._hold(symbol, ohlcv_df, "no M15 breakout")
        if action == SignalAction.SELL and not m15_breakout_short(lows, close,
                                                                  cfg.breakout_bars):
            return self._hold(symbol, ohlcv_df, "no M15 breakdown")

        # ---- risk levels from M15 ATR ----
        levels = risk_levels(action, close, float(cur["atr"]) if "atr" in cur else np.nan,
                             cfg)
        if levels is None:
            return self._hold(symbol, ohlcv_df, "degenerate ATR stop")
        sl, tp = levels

        sig = TradeSignal(
            symbol=symbol,
            action=action,
            entry_price=close,
            stop_loss=round(sl, 5),
            take_profit=round(tp, 5),
            rationale=("AlphaV2-%s H4=%s H1pullback=%.2fATR M15breakout=%d"
                       % (cfg.name, "up" if reg.long_ok else "down",
                          cfg.pullback_atr, cfg.breakout_bars)),
        )
        # the simulator reads this to enforce the time stop
        sig.time_stop = cfg.time_stop_bars
        return sig

    def _hold(self, symbol: str, df: pd.DataFrame, why: str) -> TradeSignal:
        px = float(df["close"].iloc[-1]) if len(df) else 0.0
        return TradeSignal(symbol=symbol, action=SignalAction.HOLD,
                           entry_price=px, stop_loss=0.0, take_profit=0.0,
                           rationale=why)


def attach_m15_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """M15 ATR, used for stop sizing."""
    out = df.copy()
    hl = out["high"] - out["low"]
    hc = (out["high"] - out["close"].shift()).abs()
    lc = (out["low"] - out["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    out["atr"] = tr.rolling(period).mean()
    return out
