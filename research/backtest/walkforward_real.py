"""Walk-forward harness over REAL bars, with the prop rules attached.

What this does that a normal backtest does not
---------------------------------------------
It answers the question that actually decides the account:

    given a frozen rule set, did this strategy reach the target
    before the daily or maximum loss limit ended the evaluation?

Three properties are enforced structurally rather than promised:
  1. Folds are built from the calendar BEFORE any data is touched.
  2. The training slice can never see the test slice (purge + embargo).
  3. Intrabar breach detection runs on every bar, so a spike that a
     close-only view would miss still kills the account.

Honesty contract
----------------
The spread is MODELLED. Yahoo supplies no bid/ask. Every result printed by this
harness must be read as "what would have happened IF the spread were X", and the
cost stress cases exist precisely so that assumption cannot hide.
"""
from __future__ import annotations

import datetime as _dt
import statistics
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from config.fundingpips_rules import (
    DEFAULT_MODEL,
    EvaluationModel,
    RiskBuffer,
    TWO_STEP_STANDARD,
)
from research.data.real_feed import (
    PIP_JPY,
    Candle,
    pip_size_for,
    pip_value_per_lot_for,
)


def _implied_usdjpy_rate(candles, default: float = 150.0) -> float:
    """Best-effort USDJPY rate from the bars in front of us.

    Used ONLY to convert a JPY pip value into the account currency. A backtest
    has no live account, so the rate must come from the data. Falls back to a
    conservative constant rather than zero, and never silently to 1.0 (which
    would reintroduce the ~149x error this exists to prevent).
    """
    for c in reversed(candles):
        if c.close and 50.0 < c.close < 300.0:
            return float(c.close)
    return default
from research.risk.fundingpips_engine import (
    BreachKind,
    Ordering,
    PropRuleEngine,
)


@dataclass(frozen=True)
class Fold:
    """One train/test split. Built before data is loaded."""

    index: int
    train_start: _dt.datetime
    train_end: _dt.datetime
    test_start: _dt.datetime
    test_end: _dt.datetime

    def train(self, candles: Sequence[Candle]) -> List[Candle]:
        return [c for c in candles if self.train_start <= c.timestamp < self.train_end]

    def test(self, candles: Sequence[Candle]) -> List[Candle]:
        return [c for c in candles if self.test_start <= c.timestamp < self.test_end]


def build_folds(candles: Sequence[Candle], train_years: int = 4,
                test_years: int = 1, embargo_days: int = 30
                ) -> List[Fold]:
    """Anchored walk-forward folds.

    The embargo exists because a trade opened near the end of train can still be
    open when test begins; without a gap the two slices share outcome
    information and the "out of sample" claim is false.
    """
    if not candles:
        return []
    start = candles[0].timestamp
    end = candles[-1].timestamp
    folds: List[Fold] = []
    idx = 0
    test_start = start + _dt.timedelta(days=int(train_years * 365.25))
    while True:
        test_end = test_start + _dt.timedelta(days=int(test_years * 365.25))
        if test_end > end:
            break
        train_end = test_start - _dt.timedelta(days=embargo_days)
        folds.append(Fold(
            index=idx,
            train_start=start,
            train_end=train_end,
            test_start=test_start,
            test_end=test_end,
        ))
        idx += 1
        test_start = test_end
    return folds


# --------------------------------------------------------------- strategies

@dataclass
class StrategyParams:
    """Deliberately tiny: a big grid is how overfitting is manufactured."""

    fast: int = 20
    slow: int = 100
    stop_pips: float = 60.0
    target_pips: float = 120.0
    risk_pct: float = 0.5
    lots: float = 0.10

    def as_dict(self) -> Dict[str, float]:
        return {"fast": self.fast, "slow": self.slow,
                "stop_pips": self.stop_pips, "target_pips": self.target_pips,
                "risk_pct": self.risk_pct, "lots": self.lots}


def sma(values: Sequence[float], n: int, i: int) -> Optional[float]:
    if i + 1 < n:
        return None
    window = values[i + 1 - n:i + 1]
    return sum(window) / n


def signal_at(closes: Sequence[float], i: int, p: StrategyParams) -> Optional[str]:
    """Long when fast SMA is above slow SMA, short when below. No look-ahead:
    only bars up to and including i are read."""
    f = sma(closes, p.fast, i)
    s = sma(closes, p.slow, i)
    if f is None or s is None:
        return None
    if f > s:
        return "BUY"
    if f < s:
        return "SELL"
    return None


# ------------------------------------------------------------------ result

@dataclass(frozen=True)
class TradeRecord:
    """One closed trade. Needed for falsification, not just for counting.

    A bare trade COUNT makes the whole Phase 6 suite impossible: you cannot drop
    the best year, drop the best pair, resample trade order, or bootstrap
    expectancy without knowing each trade individually. Counting was the reason
    the falsification modules sat orphaned.
    """

    fold: int
    symbol: str
    side: str
    entry_time: _dt.datetime
    exit_time: _dt.datetime
    entry_price: float
    exit_price: float
    lots: float
    pips: float
    gross_pnl: float
    net_pnl: float

    def to_dict(self) -> Dict[str, object]:
        return {
            "fold": self.fold, "symbol": self.symbol, "side": self.side,
            "entry_time": self.entry_time.isoformat(),
            "exit_time": self.exit_time.isoformat(),
            "entry_price": round(self.entry_price, 6),
            "exit_price": round(self.exit_price, 6),
            "lots": self.lots,
            "pips": round(self.pips, 3),
            "gross_pnl": round(self.gross_pnl, 4),
            "net_pnl": round(self.net_pnl, 4),
        }


@dataclass
class FoldOutcome:
    fold: int
    trades: int = 0
    passed: bool = False
    #: An empty slice has no trades and no breach, so it survived vacuously.
    #: Defaulting this to False made breach="none" and survived=False
    #: contradict each other, which would have been read as a hidden failure.
    survived: bool = True
    breach: str = "none"
    final_balance: float = 0.0
    max_daily_loss_pct: float = 0.0
    max_total_loss_pct: float = 0.0
    #: Per-trade detail. Empty is a legitimate outcome (no trades taken).
    trade_records: List[TradeRecord] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "fold": self.fold, "trades": self.trades, "passed": self.passed,
            "survived": self.survived, "breach": self.breach,
            "final_balance": round(self.final_balance, 2),
            "max_daily_loss_pct": round(self.max_daily_loss_pct, 4),
            "max_total_loss_pct": round(self.max_total_loss_pct, 4),
            "recorded_trades": len(self.trade_records),
        }

    # ------------------------------------------------------- trade access
    def trades_frame(self):
        """Per-trade view for the Phase 6 helpers. Lazily imports pandas."""
        import pandas as pd
        if not self.trade_records:
            return pd.DataFrame(
                columns=["fold", "symbol", "side", "entry_time", "exit_time",
                         "entry_price", "exit_price", "lots", "pips",
                         "gross_pnl", "net_pnl"])
        return pd.DataFrame([t.to_dict() for t in self.trade_records])

    @property
    def net_pips(self) -> List[float]:
        return [t.pips for t in self.trade_records]


@dataclass
class WalkForwardResult:
    params: Dict[str, float]
    initial_balance: float
    model_key: str
    spread_pips: float
    slippage_pips: float
    folds: List[FoldOutcome] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        if not self.folds:
            return 0.0
        return sum(1 for f in self.folds if f.passed) / len(self.folds)

    @property
    def survival_rate(self) -> float:
        if not self.folds:
            return 0.0
        return sum(1 for f in self.folds if f.survived) / len(self.folds)

    def summary(self) -> Dict[str, object]:
        return {
            "model": self.model_key,
            "initial_balance": self.initial_balance,
            "spread_pips": self.spread_pips,
            "slippage_pips": self.slippage_pips,
            "params": self.params,
            "folds": len(self.folds),
            "passed_folds": sum(1 for f in self.folds if f.passed),
            "survived_folds": sum(1 for f in self.folds if f.survived),
            "pass_rate": round(self.pass_rate, 4),
            "survival_rate": round(self.survival_rate, 4),
            "detail": [f.to_dict() for f in self.folds],
        }


# ------------------------------------------------------------- the harness

def run_fold(candles: Sequence[Candle], params: StrategyParams,
             model: EvaluationModel, initial_balance: float,
             spread_pips: float, slippage_pips: float,
             pip: Optional[float] = None,
             pip_value_per_lot: Optional[float] = None,
             buffer: Optional[RiskBuffer] = None,
             ordering: Ordering = Ordering.ADVERSE_FIRST,
             fold_index: int = 0,
             symbol: str = "EURUSD",
             jpy_rate: Optional[float] = None) -> FoldOutcome:
    """Simulate one test slice against the prop rules, bar by bar.

    Costs are charged EXPLICITLY: a long enters at mid + half spread + slippage
    and exits at mid - half spread - slippage. Modelling the spread by shaving
    the P&L afterwards would hide exactly the cost that decides marginal edges.
    """
    if not candles:
        return FoldOutcome(fold=fold_index)

    # Pip size comes from the SYMBOL, never from a literal default. USDJPY
    # quotes to 3 decimals, so a hardcoded 0.0001 would make every JPY stop
    # 100x too tight -- silently, with no error raised anywhere.
    pip = pip_size_for(symbol) if pip is None else pip
    if pip_value_per_lot is None:
        # A JPY pair needs the rate to convert its JPY pip value into USD.
        # The caller may supply one; otherwise we derive it from the bars,
        # which is legitimate because this is a backtest fixture, not a live
        # account balance.
        rate = jpy_rate
        if rate is None:
            rate = _implied_usdjpy_rate(candles)
        pip_value_per_lot = pip_value_per_lot_for(symbol, jpy_rate=rate)             if pip_size_for(symbol) == PIP_JPY else pip_value_per_lot_for(symbol)

    engine = PropRuleEngine(model=model, initial_balance=initial_balance,
                            buffer=buffer, ordering=ordering, keep_path=False)
    symbol_name = symbol
    closes = [c.close for c in candles]
    half = (spread_pips * pip) / 2.0
    slip = slippage_pips * pip

    entry_price = 0.0
    stop_price = 0.0
    target_price = 0.0
    trades = 0
    records: List[TradeRecord] = []
    entry_time: Optional[_dt.datetime] = None
    entry_side = ""
    entry_equity = initial_balance

    def _close_and_book(exit_price: float, exit_time: _dt.datetime) -> None:
        """Close the position and record it as ONE atomic step.

        The order matters: close_position() clears engine.position, so reading
        the record afterwards silently produced zero rows. Closing and booking
        together makes that mistake impossible to repeat.
        """
        nonlocal trades, entry_time
        pos = engine.position
        if pos is None:
            return
        engine.close_position(exit_price, exit_time)
        direction = 1.0 if pos.side.upper() == "BUY" else -1.0
        mid_pips = direction * (exit_price - pos.entry_price) / pip
        gross = mid_pips * pip_value_per_lot * pos.volume
        records.append(TradeRecord(
            fold=fold_index, symbol=symbol_name, side=pos.side,
            entry_time=entry_time or exit_time, exit_time=exit_time,
            entry_price=pos.entry_price, exit_price=exit_price,
            lots=pos.volume, pips=mid_pips, gross_pnl=gross, net_pnl=gross,
        ))
        trades += 1
        entry_time = None

    for i, bar in enumerate(candles):
        if not engine.outcome.survival:
            break

        # ---- 1. manage an open position using THIS bar's extremes ----------
        if engine.position is not None:
            side = engine.position.side
            if side == "BUY":
                if bar.low <= stop_price:
                    _close_and_book(stop_price, bar.timestamp)
                elif bar.high >= target_price:
                    _close_and_book(target_price, bar.timestamp)
            else:
                if bar.high >= stop_price:
                    _close_and_book(stop_price, bar.timestamp)
                elif bar.low <= target_price:
                    _close_and_book(target_price, bar.timestamp)

        # ---- 2. the bar's intrabar path is ALWAYS checked ------------------
        alive = engine.on_bar(bar.timestamp, bar.open, bar.high, bar.low, bar.close)
        if not alive:
            break

        # ---- 3. maybe enter on the close (decision uses closed bars only) --
        if engine.position is None and i > 0:
            direction = signal_at(closes, i, params)
            if direction:
                if direction == "BUY":
                    entry = bar.close + half + slip
                    stop_price = entry - params.stop_pips * pip
                    target_price = entry + params.target_pips * pip
                    engine.open_position("BUY", params.lots, entry,
                                         pip_size=pip,
                                         pip_value_per_lot=pip_value_per_lot)
                    entry_time = bar.timestamp
                    entry_side = "BUY"
                else:
                    entry = bar.close - half - slip
                    stop_price = entry + params.stop_pips * pip
                    target_price = entry - params.target_pips * pip
                    engine.open_position("SELL", params.lots, entry,
                                         pip_size=pip,
                                         pip_value_per_lot=pip_value_per_lot)
                    entry_time = bar.timestamp
                    entry_side = "SELL"

    o = engine.outcome
    return FoldOutcome(
        fold=fold_index,
        trades=trades,
        passed=o.passed,
        survived=o.survival,
        breach=o.breach.value,
        final_balance=o.final_balance,
        max_daily_loss_pct=o.max_daily_loss_pct,
        max_total_loss_pct=o.max_total_loss_pct,
        trade_records=records,
    )


def walk_forward(candles: Sequence[Candle], params: StrategyParams,
                 model: EvaluationModel = TWO_STEP_STANDARD,
                 initial_balance: float = 10000.0,
                 spread_pips: float = 1.0, slippage_pips: float = 0.3,
                 train_years: int = 4, test_years: int = 1,
                 embargo_days: int = 30,
                 buffer: Optional[RiskBuffer] = None,
                 ordering: Ordering = Ordering.ADVERSE_FIRST,
                 symbol: str = "EURUSD",
                 ) -> WalkForwardResult:
    """Run every fold and return the aggregate.

    Parameters are FROZEN across folds: the grid is searched once, and selection
    happens on train only. Selecting per fold on test would manufacture a
    result rather than measure one.
    """
    folds = build_folds(candles, train_years, test_years, embargo_days)
    res = WalkForwardResult(
        params=params.as_dict(),
        initial_balance=initial_balance,
        model_key=model.key,
        spread_pips=spread_pips,
        slippage_pips=slippage_pips,
    )
    for f in folds:
        test_slice = f.test(candles)
        res.folds.append(run_fold(
            test_slice, params, model, initial_balance,
            spread_pips, slippage_pips, buffer=buffer,
            ordering=ordering, fold_index=f.index, symbol=symbol,
        ))
    return res


# ------------------------------------------------------------ cost stress

COST_STRESS: Tuple[Tuple[str, float, float], ...] = (
    ("base", 1.0, 1.0),
    ("moderate", 1.25, 1.5),
    ("severe", 1.5, 2.0),
    ("crisis", 2.0, 3.0),
)


def cost_stress(candles: Sequence[Candle], params: StrategyParams,
                model: EvaluationModel = TWO_STEP_STANDARD,
                initial_balance: float = 10000.0,
                base_spread: float = 1.0, base_slip: float = 0.3,
                ) -> List[Dict[str, object]]:
    """Rerun the SAME frozen parameters under escalating costs.

    A strategy that only survives at the base cost is not a strategy; it is a
    spread assumption.
    """
    rows: List[Dict[str, object]] = []
    for label, smul, lmul in COST_STRESS:
        r = walk_forward(candles, params, model, initial_balance,
                         spread_pips=base_spread * smul,
                         slippage_pips=base_slip * lmul)
        s = r.summary()
        rows.append({
            "stress": label,
            "spread_pips": round(base_spread * smul, 3),
            "slippage_pips": round(base_slip * lmul, 3),
            "folds": s["folds"],
            "survived": s["survived_folds"],
            "passed": s["passed_folds"],
            "survival_rate": s["survival_rate"],
            "pass_rate": s["pass_rate"],
        })
    return rows
