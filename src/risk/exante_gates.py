"""Ex-ante gates. Interfaces now; final calibration is Phase 3-4.

The rule this module exists to enforce (protocol §4):

    ExpectedEdge_ex_ante(t) > ExpectedCost(t) + SafetyMargin

Every input must be computable from information strictly BEFORE the trade.
Passing an OOS-derived number into `expected_edge` is a protocol violation, and the
API is shaped to make that hard: an `ExAnteEstimate` can only be built from a
TRAINING window, and it carries the window it was built from so the caller cannot
silently forget where the number came from.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Cost gate
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CostEstimate:
    """Ex-ante expected round-trip cost for a trade, in pips."""
    spread_pips: float
    slippage_pips: float          # round trip (both sides)
    commission_pips: float        # commission converted to pip-equivalent
    financing_pips: float         # expected overnight cost over the intended hold

    @property
    def total_pips(self) -> float:
        return (self.spread_pips + self.slippage_pips
                + self.commission_pips + self.financing_pips)


@dataclass(frozen=True)
class ExAnteEstimate:
    """An expected-edge estimate and the provenance of the data behind it.

    `trained_on` is not decoration. It records the window the estimate was derived
    from so that an ex-post number cannot be passed off as ex-ante.
    """
    edge_pips: float
    trained_on: tuple                       # (start, end) of the fitting window
    n_observations: int

    def __post_init__(self) -> None:
        if self.trained_on is None or len(self.trained_on) != 2:
            raise ValueError("ExAnteEstimate requires an explicit training window")
        if self.n_observations <= 0:
            raise ValueError("ExAnteEstimate requires a positive observation count")


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reason: str
    edge_pips: float
    cost_pips: float
    required_pips: float


def estimate_cost(spread_pips: float, slippage_pips: float = 0.2,
                  commission_per_lot_per_side: float = 3.0,
                  lot_size: float = 0.10, pip_value_per_lot: float = 10.0,
                  expected_nights: float = 0.0,
                  swap_pips_per_night: float = -0.5) -> CostEstimate:
    """Round-trip cost in pips. Commission is converted to a pip equivalent."""
    cpp = pip_value_per_lot * lot_size
    commission_pips = (2.0 * commission_per_lot_per_side * lot_size / cpp) if cpp > 0 else 0.0
    financing = abs(swap_pips_per_night) * expected_nights
    return CostEstimate(spread_pips=spread_pips,
                        slippage_pips=2.0 * slippage_pips,
                        commission_pips=commission_pips,
                        financing_pips=financing)


def cost_gate(est: ExAnteEstimate, cost: CostEstimate,
              safety_multiple: float = 2.0) -> GateDecision:
    """Allow a trade only if the ex-ante edge clears cost with a safety margin.

    `safety_multiple=2.0` means we require the estimated edge to be at least twice
    the expected cost, not merely to exceed it. Break-even is not an edge.
    """
    required = cost.total_pips * safety_multiple
    allowed = est.edge_pips > required
    reason = ("edge %.2f pip > required %.2f pip (%.1fx cost %.2f)"
              % (est.edge_pips, required, safety_multiple, cost.total_pips))
    if not allowed:
        reason = ("NO TRADE: edge %.2f pip <= required %.2f pip (%.1fx cost %.2f)"
                  % (est.edge_pips, required, safety_multiple, cost.total_pips))
    return GateDecision(allowed=allowed, reason=reason, edge_pips=est.edge_pips,
                        cost_pips=cost.total_pips, required_pips=required)


def edge_from_training(window_pips: Sequence[float], trained_on: tuple) -> ExAnteEstimate:
    """Build an ex-ante edge estimate from a TRAINING window only.

    Uses the lower confidence bound rather than the mean, so a noisy estimate with
    few observations cannot masquerade as a large edge. This is deliberately
    conservative: it is the mechanism that stops a thin sample from being traded.
    """
    x = np.asarray([v for v in window_pips if np.isfinite(v)], dtype=float)
    if len(x) == 0:
        raise ValueError("cannot estimate edge from an empty training window")
    mu = float(x.mean())
    if len(x) > 1:
        se = float(x.std(ddof=1) / np.sqrt(len(x)))
        lo = mu - 1.645 * se          # one-sided 95% lower bound
    else:
        lo = 0.0
    return ExAnteEstimate(edge_pips=lo, trained_on=trained_on, n_observations=len(x))


# ---------------------------------------------------------------------------
# VR regime veto (ex-ante)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class VrThresholds:
    """Frozen thresholds derived from a TRAINING window only."""
    sd_vr_p20: float
    mean_vr_eps: float
    trained_on: tuple


def fit_vr_thresholds(sd_vr_train: Sequence[float], mean_vr_train: Sequence[float],
                      trained_on: tuple, percentile: float = 20.0,
                      eps: float = 0.05) -> VrThresholds:
    """Fit the veto thresholds on train data, then freeze them.

    Protocol §7: never optimise the threshold against results. A raw number such as
    `sdVR < 0.01372` is forbidden; a percentile of the TRAINING distribution is fine.
    """
    s = np.asarray([v for v in sd_vr_train if np.isfinite(v)], dtype=float)
    if len(s) == 0:
        raise ValueError("cannot fit VR thresholds on an empty training window")
    return VrThresholds(sd_vr_p20=float(np.percentile(s, percentile)),
                        mean_vr_eps=eps, trained_on=trained_on)


def vr_veto(sd_vr_now: float, mean_vr_now: float,
            th: VrThresholds) -> GateDecision:
    """Return allowed=False when the regime is too close to a random walk.

    Two conditions, both from the protocol:
      * dispersion collapse  -> sd(VR) below the training P20
      * basket mean VR ~ 1   -> no exploitable drift or reversion
    """
    if not np.isfinite(sd_vr_now) or not np.isfinite(mean_vr_now):
        return GateDecision(False, "VR veto: undefined statistic", 0.0, 0.0, 0.0)
    if mean_vr_now < th.mean_vr_eps or abs(mean_vr_now - 1.0) < th.mean_vr_eps:
        return GateDecision(
            False,
            "VR veto: basket mean %.4f within %.2f of random walk"
            % (mean_vr_now, th.mean_vr_eps), 0.0, 0.0, 0.0)
    if sd_vr_now < th.sd_vr_p20:
        return GateDecision(
            False,
            "VR veto: sd(VR) %.5f below train P20 %.5f (cross-section too uniform)"
            % (sd_vr_now, th.sd_vr_p20), 0.0, 0.0, 0.0)
    return GateDecision(True, "VR regime OK", 0.0, 0.0, 0.0)


# ---------------------------------------------------------------------------
# Session filter
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SessionFilter:
    """Liquid-hours gate. Defaults to the London/NY overlap, avoiding rollover."""
    start_hour_utc: int = 7
    end_hour_utc: int = 20
    avoid_rollover: bool = True

    def allows(self, ts: pd.Timestamp) -> bool:
        t = pd.Timestamp(ts)
        if t.tzinfo is None:
            t = t.tz_localize("UTC")
        t = t.tz_convert("UTC")
        if t.dayofweek >= 5:
            return False
        if not (self.start_hour_utc <= t.hour < self.end_hour_utc):
            return False
        if self.avoid_rollover and t.hour == 21:
            return False
        return True
