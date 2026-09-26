"""Phase 4: ex-ante cost gate and session policy.

Hard rule enforced structurally: the DECISION function accepts only ex-ante inputs.
Realised P&L lives on the result record but is NOT a parameter of any decision, so
leaking the outcome into the choice is type-impossible rather than merely discouraged.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np
import pandas as pd

from research.protocol.frozen_config import Frozen, TrainingWindow


@dataclass(frozen=True)
class CostModel:
    """Round-trip cost in pips, decomposed. All ex-ante."""
    spread_pips: float = 1.0
    slippage_pips_round_trip: float = 0.4
    commission_pips: float = 0.6
    financing_pips: float = 0.0

    @property
    def total_pips(self) -> float:
        return (self.spread_pips + self.slippage_pips_round_trip
                + self.commission_pips + self.financing_pips)

    def scaled(self, factor: float) -> "CostModel":
        """Phase 6 stress only (x1.5, x2). Never used during a clean run."""
        return CostModel(self.spread_pips * factor,
                         self.slippage_pips_round_trip * factor,
                         self.commission_pips * factor,
                         self.financing_pips * factor)


@dataclass
class GateRecord:
    """What the decision saw, and the outcome it did NOT see."""
    expected_edge_train: float
    expected_cost_at_decision: float
    safety_multiple: float
    required_edge: float
    decision_allowed: bool
    decision_reason: str
    realized_pnl: Optional[float] = None


class CostGate(Frozen):
    """Allows a trade only when the ex-ante edge clears a multiple of expected cost.

    safety_multiple > 1 means break-even is explicitly not enough.
    """

    def __init__(self, safety_multiple: float = 2.0):
        super().__init__()
        self.safety_multiple = float(safety_multiple)
        self.edge_estimate = float("nan")
        self.n_train = 0

    def fit(self, train_trade_pips: Sequence[float]) -> "CostGate":
        vals = np.asarray([v for v in train_trade_pips if np.isfinite(v)], dtype=float)
        if len(vals) == 0:
            raise ValueError("cannot fit the cost gate without training trades")
        mu = float(vals.mean())
        se = float(vals.std(ddof=1) / np.sqrt(len(vals))) if len(vals) > 1 else 0.0
        self.edge_estimate = mu - 1.645 * se
        self.n_train = int(len(vals))
        self._freeze(TrainingWindow(pd.Timestamp("1970-01-01", tz="UTC"),
                                    pd.Timestamp("1970-01-01", tz="UTC"), len(vals)),
                     safety_multiple=self.safety_multiple,
                     edge_estimate=self.edge_estimate, n=len(vals))
        return self

    def decide(self, cost: CostModel) -> GateRecord:
        """Ex-ante decision. The signature deliberately omits any realised outcome."""
        self.assert_frozen()
        required = cost.total_pips * self.safety_multiple
        allowed = bool(self.edge_estimate > required)
        if allowed:
            reason = ("allowed: ex-ante edge %.3f pip > required %.3f (%.1fx cost %.3f)"
                      % (self.edge_estimate, required, self.safety_multiple,
                         cost.total_pips))
        else:
            reason = ("NO TRADE: ex-ante edge %.3f pip <= required %.3f (%.1fx cost %.3f)"
                      % (self.edge_estimate, required, self.safety_multiple,
                         cost.total_pips))
        return GateRecord(expected_edge_train=self.edge_estimate,
                          expected_cost_at_decision=cost.total_pips,
                          safety_multiple=self.safety_multiple,
                          required_edge=required, decision_allowed=allowed,
                          decision_reason=reason)


class SessionPolicy(Frozen):
    """Liquid-hours policy, frozen as part of the configuration."""

    def __init__(self, start_hour_utc: int = 7, end_hour_utc: int = 20,
                 avoid_rollover: bool = True, block_weekend: bool = True):
        super().__init__()
        self.start_hour_utc = int(start_hour_utc)
        self.end_hour_utc = int(end_hour_utc)
        self.avoid_rollover = bool(avoid_rollover)
        self.block_weekend = bool(block_weekend)
        self._freeze(TrainingWindow(pd.Timestamp("1970-01-01", tz="UTC"),
                                    pd.Timestamp("1970-01-01", tz="UTC"), 0),
                     start_hour_utc=self.start_hour_utc,
                     end_hour_utc=self.end_hour_utc)

    def allows(self, ts) -> bool:
        t = pd.Timestamp(ts)
        if t.tzinfo is None:
            t = t.tz_localize("UTC")
        t = t.tz_convert("UTC")
        if self.block_weekend and t.dayofweek >= 5:
            return False
        if not (self.start_hour_utc <= t.hour < self.end_hour_utc):
            return False
        if self.avoid_rollover and t.hour == 21:
            return False
        return True
