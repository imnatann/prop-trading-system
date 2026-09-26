"""Phase 3: VR regime veto. Fit on TRAIN, then frozen.

Protocol section 7: VR is not a signal, it is a veto. Thresholds come from the
TRAINING distribution of the statistic, never from a number that happened to work.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np
import pandas as pd

from research.protocol.frozen_config import Frozen, TrainingWindow


@dataclass
class VetoDecision:
    allowed: bool
    reason: str
    sd_vr: float
    mean_vr: float
    threshold_sd: float


class VRVeto(Frozen):
    """Regime veto fitted on train only.

    After fit(), the percentile threshold is immutable. allow() recomputes the
    statistic from supplied data but NEVER re-derives a threshold.
    """

    def __init__(self, percentile: float = 20.0, eps: float = 0.05):
        super().__init__()
        self.percentile = float(percentile)
        self.eps = float(eps)
        self.sd_threshold = float("nan")
        self.train_sd_median = float("nan")

    def fit(self, train_sd_vr: Sequence[float]) -> "VRVeto":
        vals = np.asarray([v for v in train_sd_vr if np.isfinite(v)], dtype=float)
        if len(vals) == 0:
            raise ValueError("cannot fit VR veto on an empty training sample")
        self.sd_threshold = float(np.percentile(vals, self.percentile))
        self.train_sd_median = float(np.median(vals))
        self._freeze(TrainingWindow(pd.Timestamp("1970-01-01", tz="UTC"),
                                    pd.Timestamp("1970-01-01", tz="UTC"), len(vals)),
                     percentile=self.percentile, eps=self.eps,
                     sd_threshold=self.sd_threshold,
                     train_sd_median=self.train_sd_median)
        return self

    def allow(self, sd_vr_now: float, mean_vr_now: float) -> VetoDecision:
        self.assert_frozen()
        if not np.isfinite(sd_vr_now) or not np.isfinite(mean_vr_now):
            return VetoDecision(False, "undefined VR statistic", sd_vr_now,
                                mean_vr_now, self.sd_threshold)
        if abs(mean_vr_now - 1.0) < self.eps:
            return VetoDecision(False,
                                "basket mean VR %.4f within %.2f of a random walk"
                                % (mean_vr_now, self.eps), sd_vr_now, mean_vr_now,
                                self.sd_threshold)
        if sd_vr_now < self.sd_threshold:
            return VetoDecision(False,
                                "sd(VR) %.5f below train P%.0f %.5f"
                                % (sd_vr_now, self.percentile, self.sd_threshold),
                                sd_vr_now, mean_vr_now, self.sd_threshold)
        return VetoDecision(True, "regime dispersive enough to trade", sd_vr_now,
                            mean_vr_now, self.sd_threshold)


def cross_sectional_sd(series_by_pair: Sequence[Sequence[float]]) -> float:
    """Standard deviation of a per-pair statistic across the cross-section.

    Kept as a pure function so the veto can be unit tested with no market data.
    """
    last = []
    for s in series_by_pair:
        arr = np.asarray([x for x in s if np.isfinite(x)], dtype=float)
        if len(arr):
            last.append(float(arr[-1]))
    if len(last) < 2:
        return float("nan")
    return float(np.std(last, ddof=0))


def cross_sectional_mean(series_by_pair: Sequence[Sequence[float]]) -> float:
    last = []
    for s in series_by_pair:
        arr = np.asarray([x for x in s if np.isfinite(x)], dtype=float)
        if len(arr):
            last.append(float(arr[-1]))
    if not last:
        return float("nan")
    return float(np.mean(last))
