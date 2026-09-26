"""Prop-firm rule engine with INTRA-BAR breach detection.

The problem this solves
----------------------
A conventional backtest knows the close of each bar. FundingPips does not care
about closes. It terminates the account the moment EQUITY touches a floor at ANY
instant, including floating loss inside a bar that later recovers. So a strategy
can show a beautiful equity curve in a close-only backtest and still have been
dead on day 9.

The documented rule text is explicit:

    "Equity cannot fall by more than 5% of that baseline at any point during
     the day, including floating losses from open trades."

Therefore the simulator needs an intrabar equity PATH, not a close series.

How the path is reconstructed (and why it is CONSERVATIVE by default)
--------------------------------------------------------------------
From OHLC we do not know the order in which high and low occurred. Two
assumptions are possible:

  * ADVERSE_FIRST (default): a bar that contains both the stop and a new high is
    assumed to have gone AGAINST us first. This front-loads the drawdown, so it
    detects breaches EARLIER than reality. For a failure test that is the correct
    direction to be wrong in: better to reject a strategy that might have
    survived than to pass one that might have died.
  * ADVERSE_LAST: the optimistic ordering. Offered for sensitivity analysis only.
    A verdict that flips between the two orderings is not a verdict.

This module DELIBERATELY does not import the execution layer, MT5, or any
strategy. It is pure arithmetic over numbers it is given, which is what makes it
testable without a broker and impossible to entangle with order routing.
"""
from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

from config.fundingpips_rules import (
    DEFAULT_BUFFER,
    DEFAULT_MODEL,
    INACTIVITY_BREACH_DAYS,
    PLATFORM_UTC_OFFSET_HOURS,
    PROFIT_CONCENTRATION_FRACTION,
    EvaluationModel,
    RiskBuffer,
    platform_trading_day,
)


class BreachKind(str, Enum):
    """How an evaluation ended. NONE means it was still alive."""

    NONE = "none"
    DAILY_LOSS = "daily_loss"
    MAX_LOSS = "max_loss"
    INACTIVITY = "inactivity"


class SoftStop(str, Enum):
    """Our own internal stops. These do NOT kill the account."""

    NONE = "none"
    SOFT_DAILY = "soft_daily"
    SOFT_MAX_LOSS = "soft_max_loss"


class Ordering(str, Enum):
    """Intrabar assumption, see module docstring."""

    ADVERSE_FIRST = "adverse_first"
    ADVERSE_LAST = "adverse_last"


@dataclass
class OpenPosition:
    """Minimal position view needed for equity reconstruction."""

    side: str                 # "BUY" | "SELL"
    volume: float
    entry_price: float
    pip_size: float = 0.0001
    pip_value_per_lot: float = 10.0

    def pnl_at(self, price: float) -> float:
        """Unrealised P&L in account currency at the given price."""
        direction = 1.0 if self.side.upper() == "BUY" else -1.0
        pips = direction * (price - self.entry_price) / self.pip_size
        return pips * self.pip_value_per_lot * self.volume


@dataclass
class TickPoint:
    """One reconstructed equity observation."""

    timestamp: _dt.datetime
    balance: float
    equity: float
    note: str = ""


@dataclass
class BreachEvent:
    """A single rule violation, with enough context to be auditable."""

    kind: BreachKind
    timestamp: _dt.datetime
    equity: float
    balance: float
    limit_price: float
    loss_pct: float
    detail: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "kind": self.kind.value,
            "timestamp": self.timestamp.isoformat(),
            "equity": round(self.equity, 2),
            "balance": round(self.balance, 2),
            "limit_price": round(self.limit_price, 2),
            "loss_pct": round(self.loss_pct, 4),
            "detail": self.detail,
        }


@dataclass
class EvaluationOutcome:
    """Result of simulating one evaluation attempt."""

    model_key: str
    initial_balance: float
    final_balance: float
    final_equity: float
    breach: BreachKind = BreachKind.NONE
    breach_time: Optional[_dt.datetime] = None
    breach_event: Optional[BreachEvent] = None
    soft_stop: SoftStop = SoftStop.NONE
    soft_stop_time: Optional[_dt.datetime] = None
    phase1_reached: bool = False
    phase1_time: Optional[_dt.datetime] = None
    phase2_reached: bool = False
    phase2_time: Optional[_dt.datetime] = None
    days_to_target: Optional[int] = None
    max_daily_loss_pct: float = 0.0
    max_total_loss_pct: float = 0.0
    largest_day_profit: float = 0.0
    total_profit: float = 0.0
    equity_path: List[TickPoint] = field(default_factory=list)
    events: List[BreachEvent] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True only if the required phases completed with no breach."""
        if self.breach is not BreachKind.NONE:
            return False
        if self.model_key == "1_step_flex":
            return self.phase1_reached
        return self.phase1_reached and self.phase2_reached

    @property
    def survival(self) -> bool:
        return self.breach is BreachKind.NONE

    @property
    def largest_day_profit_share(self) -> float:
        """Share of total profit contributed by the best single day."""
        if self.total_profit <= 0:
            return 0.0
        return self.largest_day_profit / self.total_profit

    def concentration_triggered(self) -> bool:
        """True if one day alone exceeds the concentration fraction.

        FundingPips groups by TRADE IDEA, not by day, so this is a strict
        upper-bound proxy: if a single DAY cannot breach the threshold then no
        trade idea inside it can either. It is a screen, not a substitute.
        """
        return self.largest_day_profit_share > PROFIT_CONCENTRATION_FRACTION

    def summary(self) -> Dict[str, object]:
        return {
            "model": self.model_key,
            "passed": self.passed,
            "survived": self.survival,
            "breach": self.breach.value,
            "breach_time": self.breach_time.isoformat() if self.breach_time else None,
            "soft_stop": self.soft_stop.value,
            "phase1_reached": self.phase1_reached,
            "phase2_reached": self.phase2_reached,
            "days_to_target": self.days_to_target,
            "final_balance": round(self.final_balance, 2),
            "max_daily_loss_pct": round(self.max_daily_loss_pct, 4),
            "max_total_loss_pct": round(self.max_total_loss_pct, 4),
            "largest_day_profit_share": round(self.largest_day_profit_share, 4),
            "concentration_triggered": self.concentration_triggered(),
        }


# ------------------------------------------------------------ path builders

#: Relative tolerance for "has the target been reached" comparisons.
#: 10000 * 1.12 is 11200.000000000002 in IEEE-754, so an exact >= would tell a
#: trader who hit the target EXACTLY that they had not passed. A fail-closed
#: engine must not manufacture that ambiguity.
TARGET_RTOL = 1e-9


def _reached(balance: float, target: float, rtol: float = TARGET_RTOL) -> bool:
    """True when balance has genuinely reached the target, float error aside."""
    return balance >= target - abs(target) * rtol


def adverse_price_for(side: str, high: float, low: float) -> float:
    """The price inside the bar that is WORST for the given side.

    For a BUY the worst price is the low; for a SELL it is the high.
    """
    return low if side.upper() == "BUY" else high


def intrabar_path(open_: float, high: float, low: float, close: float,
                  side: Optional[str] = None,
                  ordering: Ordering = Ordering.ADVERSE_FIRST
                  ) -> List[Tuple[str, float]]:
    """Return a plausible (label, price) walk through one bar.

    With no open position the only meaningful observation is the close, because
    equity is flat and cannot move intrabar. With a position we emit the adverse
    extreme, then the favourable extreme, then the close. ADVERSE_FIRST
    front-loads the pain so breaches are found as early as the data allows.
    """
    if side is None:
        return [("close", close)]

    adverse = adverse_price_for(side, high, low)
    favourable = high if side.upper() == "BUY" else low
    if ordering is Ordering.ADVERSE_FIRST:
        seq = [("adverse", adverse), ("favourable", favourable), ("close", close)]
    else:
        seq = [("favourable", favourable), ("adverse", adverse), ("close", close)]

    seen: List[Tuple[str, float]] = []
    for label, price in seq:
        if seen and abs(seen[-1][1] - price) < 1e-12:
            continue
        seen.append((label, price))
    return seen


class PropRuleEngine:
    """Tracks one evaluation attempt bar by bar, checking breach INTRABAR.

    The engine is intentionally stateful and explicit: every mutation is tied to
    a timestamp, so a failed account can always be explained. It never repairs a
    breach and never retries.
    """

    def __init__(self, model: Optional[EvaluationModel] = None,
                 initial_balance: float = 10000.0,
                 buffer: Optional[RiskBuffer] = None,
                 ordering: Ordering = Ordering.ADVERSE_FIRST,
                 offset_hours: int = PLATFORM_UTC_OFFSET_HOURS,
                 keep_path: bool = True):
        self.model = model or DEFAULT_MODEL
        self.buffer = buffer or DEFAULT_BUFFER
        self.ordering = ordering
        self.offset_hours = offset_hours
        self.keep_path = keep_path

        self.initial_balance = float(initial_balance)
        self.balance = float(initial_balance)
        self.position: Optional[OpenPosition] = None

        self._day: Optional[_dt.date] = None
        self._baseline = float(initial_balance)
        self._day_open_balance = float(initial_balance)
        self._day_open_equity = float(initial_balance)

        self.phase = 1
        self._first_ts: Optional[_dt.datetime] = None
        self._last_activity: Optional[_dt.datetime] = None
        self._day_profit: Dict[_dt.date, float] = {}

        self.outcome = EvaluationOutcome(
            model_key=self.model.key,
            initial_balance=self.initial_balance,
            final_balance=self.initial_balance,
            final_equity=self.initial_balance,
        )

    # ---------------------------------------------------------------- helpers

    def equity(self, price: Optional[float] = None) -> float:
        """Equity = balance + unrealised P&L. The accounting identity."""
        if self.position is None:
            return self.balance
        ref = price if price is not None else self.position.entry_price
        return self.balance + self.position.pnl_at(ref)

    @property
    def baseline(self) -> float:
        return self._baseline

    def hard_daily_floor(self) -> float:
        return self.model.hard_daily_floor(self._baseline)

    def soft_daily_floor(self) -> float:
        return self.buffer.soft_daily_floor(self.model, self._baseline)

    def hard_max_loss_floor(self) -> float:
        return self.model.hard_max_loss_floor(self.initial_balance)

    def soft_max_loss_floor(self) -> float:
        return self.buffer.soft_max_loss_floor(self.model, self.initial_balance)

    def _reset_day_if_needed(self, ts: _dt.datetime,
                             ref_price: Optional[float] = None) -> bool:
        """Roll the daily baseline at 00:00 Platform Time.

        The day comes from the PLATFORM calendar, never the UTC calendar,
        because a 22:00 UTC bar already belongs to tomorrow's risk budget.

        ref_price is the price at which equity is marked when the baseline is
        struck. It MUST be the prevailing market price, not the position entry
        price: the baseline is deliberately the HIGHER of opening balance or
        opening equity, and marking equity at the entry price makes a profitable
        overnight position invisible, understating the baseline and thereby
        overstating the day's remaining budget. That is the documented hazard
        where "a profitable overnight position can raise the next day's loss
        baseline" -- getting it wrong here hides real breaches.

        Returns True when a roll happened.
        """
        day = platform_trading_day(ts, self.offset_hours)
        if self._day == day:
            return False
        rolled = self._day is not None
        opening_equity = self.equity(ref_price)
        self._day = day
        self._day_open_balance = self.balance
        self._day_open_equity = opening_equity
        self._baseline = max(self.balance, opening_equity)
        return rolled

    # -------------------------------------------------------------- lifecycle

    def open_position(self, side: str, volume: float, price: float,
                      pip_size: float = 0.0001,
                      pip_value_per_lot: float = 10.0) -> None:
        if self.position is not None:
            raise RuntimeError("engine models ONE position at a time; close first")
        self.position = OpenPosition(side=side, volume=volume, entry_price=price,
                                     pip_size=pip_size,
                                     pip_value_per_lot=pip_value_per_lot)

    def close_position(self, price: float, ts: _dt.datetime) -> float:
        if self.position is None:
            raise RuntimeError("no open position to close")
        pnl = self.position.pnl_at(price)
        self.balance += pnl
        self.position = None
        self._last_activity = ts
        day = platform_trading_day(ts, self.offset_hours)
        self._day_profit[day] = self._day_profit.get(day, 0.0) + pnl
        return pnl

    # ------------------------------------------------------------- the checks

    def _record_breach(self, kind: BreachKind, ts: _dt.datetime, equity: float,
                       limit: float, detail: str = "") -> None:
        loss_pct = 0.0
        if kind is BreachKind.DAILY_LOSS and self._baseline > 0:
            loss_pct = (self._baseline - equity) / self._baseline * 100.0
        elif kind is BreachKind.MAX_LOSS and self.initial_balance > 0:
            loss_pct = (self.initial_balance - equity) / self.initial_balance * 100.0
        ev = BreachEvent(kind=kind, timestamp=ts, equity=equity,
                         balance=self.balance, limit_price=limit,
                         loss_pct=loss_pct, detail=detail)
        self.outcome.events.append(ev)
        if self.outcome.breach is BreachKind.NONE:
            self.outcome.breach = kind
            self.outcome.breach_time = ts
            self.outcome.breach_event = ev

    def _check_equity(self, ts: _dt.datetime, equity: float, note: str = "") -> bool:
        """Apply the HARD rules. Returns True once the account is dead.

        Order matters: the official wording for max loss is "equity OR BALANCE
        cannot hit 10% below the starting size", so balance is tested too. With
        no open position the two are equal and one check does the work.

        Comparison is <=, matching the worked example ("cannot drop TO $95K").
        """
        hard_max = self.hard_max_loss_floor()
        if equity <= hard_max:
            self._record_breach(BreachKind.MAX_LOSS, ts, equity, hard_max, note)
            return True
        if self.balance <= hard_max:
            self._record_breach(BreachKind.MAX_LOSS, ts, self.balance, hard_max,
                                note + " (balance)")
            return True

        hard_daily = self.hard_daily_floor()
        if equity <= hard_daily:
            self._record_breach(BreachKind.DAILY_LOSS, ts, equity, hard_daily, note)
            return True
        return False

    def _check_soft(self, ts: _dt.datetime, equity: float) -> SoftStop:
        """Our own conservative stops. These never kill the account."""
        if self.outcome.soft_stop is not SoftStop.NONE:
            return self.outcome.soft_stop
        if equity <= self.soft_max_loss_floor():
            self.outcome.soft_stop = SoftStop.SOFT_MAX_LOSS
            self.outcome.soft_stop_time = ts
            self.phase = 3  # trading halted; account alive
        elif equity <= self.soft_daily_floor():
            self.outcome.soft_stop = SoftStop.SOFT_DAILY
            self.outcome.soft_stop_time = ts
        return self.outcome.soft_stop

    def _track_extremes(self, equity: float) -> None:
        if self._baseline > 0:
            daily = (self._baseline - equity) / self._baseline * 100.0
            if daily > self.outcome.max_daily_loss_pct:
                self.outcome.max_daily_loss_pct = daily
        if self.initial_balance > 0:
            total = (self.initial_balance - equity) / self.initial_balance * 100.0
            if total > self.outcome.max_total_loss_pct:
                self.outcome.max_total_loss_pct = total

    def _push_path(self, ts: _dt.datetime, equity: float, note: str) -> None:
        if self.keep_path:
            self.outcome.equity_path.append(
                TickPoint(timestamp=ts, balance=self.balance, equity=equity,
                          note=note))

    def _check_phase(self, ts: _dt.datetime) -> None:
        """Record phase completion. Passing requires the TARGET REACHED.

        The phase-2 target is measured cumulatively from the INITIAL balance,
        matching how the broker reports evaluation progress.
        """
        if not self.outcome.phase1_reached:
            if _reached(self.balance,
                        self.model.passing_balance(self.initial_balance, 1)):
                self.outcome.phase1_reached = True
                self.outcome.phase1_time = ts
                self.phase = 2
        if self.model.key == "1_step_flex":
            return
        if self.outcome.phase1_reached and not self.outcome.phase2_reached:
            if _reached(self.balance,
                        self.model.passing_balance(self.initial_balance, 2)):
                self.outcome.phase2_reached = True
                self.outcome.phase2_time = ts

    # ------------------------------------------------------------- the driver

    def on_bar(self, ts: _dt.datetime, open_: float, high: float, low: float,
               close: float) -> bool:
        """Process one bar. Returns True while the account is still alive.

        Every intrabar price is checked against BOTH hard floors. This is the
        whole point of the module: there is no close-only shortcut.
        """
        if self.outcome.breach is not BreachKind.NONE:
            return False

        if self._first_ts is None:
            self._first_ts = ts
        self._reset_day_if_needed(ts, ref_price=open_)

        side = self.position.side if self.position else None
        for label, price in intrabar_path(open_, high, low, close, side,
                                          self.ordering):
            eq = self.equity(price)
            self._track_extremes(eq)
            self._check_soft(ts, eq)
            if self._check_equity(ts, eq, label):
                self._push_path(ts, eq, label + ":BREACH")
                self._finalise(ts)
                return False
            self._push_path(ts, eq, label)

        self._check_phase(ts)
        self._finalise(ts)
        return self.outcome.breach is BreachKind.NONE

    def check_inactivity(self, ts: _dt.datetime) -> bool:
        """Inactivity breach: no COMPLETED trade within the documented window."""
        ref = self._last_activity or self._first_ts
        if ref is None:
            return False
        if (ts - ref).days > INACTIVITY_BREACH_DAYS:
            self._record_breach(BreachKind.INACTIVITY, ts, self.equity(),
                                self.equity(), "no completed trade in window")
            return True
        return False

    def _finalise(self, ts: _dt.datetime) -> None:
        o = self.outcome
        o.final_balance = self.balance
        o.final_equity = self.equity()
        o.total_profit = self.balance - self.initial_balance
        if self._day_profit:
            o.largest_day_profit = max(0.0, max(self._day_profit.values()))
        if o.phase1_reached and o.phase1_time and self._first_ts:
            o.days_to_target = max(1, (o.phase1_time - self._first_ts).days + 1)


# ------------------------------------------------------------------ accounting

def assert_equity_identity(balance: float, unrealised: float, equity: float,
                           tol: float = 1e-9) -> None:
    """equity == balance + unrealised, enforced not assumed.

    Kept here so the rule engine is validated standalone, without the simulator.
    """
    expected = balance + unrealised
    if abs(expected - equity) > tol:
        raise AssertionError(
            "equity identity violated: balance %.6f + unrealised %.6f = %.6f "
            "but equity is %.6f" % (balance, unrealised, expected, equity))


def assert_path_finite(outcome: EvaluationOutcome) -> None:
    """Reject NaN/inf in a recorded equity path before it reaches a report."""
    for point in outcome.equity_path:
        if not math.isfinite(point.equity) or not math.isfinite(point.balance):
            raise AssertionError("non-finite equity point: %r" % (point,))
