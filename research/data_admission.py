"""Data admission gate.

The research period must be the INTERSECTION of what is actually available for all
six pairs at all three timeframes:

    T_usable = INTERSECTION over (6 pairs) of INTERSECTION over (M15, H1, H4)

Using EURUSD for 10 years while another pair only has 3 would make the ">= 4/6 pairs
positive" falsification test meaningless, because the pairs would not be compared over
the same regime.

This module decides admission. It does NOT download anything and it does NOT look at
performance.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import pandas as pd

PAIRS: Tuple[str, ...] = ("EURUSD", "GBPUSD", "AUDUSD", "USDJPY", "USDCAD", "USDCHF")
TIMEFRAMES: Tuple[str, ...] = ("M15", "H1", "H4")


@dataclass
class Coverage:
    """Observed availability for one (pair, timeframe)."""
    pair: str
    timeframe: str
    earliest: Optional[pd.Timestamp]
    latest: Optional[pd.Timestamp]
    bars: int = 0

    @property
    def present(self) -> bool:
        return (self.earliest is not None and self.latest is not None
                and self.bars > 0 and self.earliest < self.latest)


@dataclass
class AdmissionReport:
    window_start: Optional[pd.Timestamp]
    window_end: Optional[pd.Timestamp]
    usable_years: float
    admitted: bool
    missing: List[Tuple[str, str]] = field(default_factory=list)
    binding: List[Tuple[str, str]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def summary(self) -> str:
        if not self.admitted:
            return ("NOT ADMITTED - missing coverage for: %s"
                    % ", ".join("%s/%s" % m for m in self.missing))
        return ("ADMITTED %s -> %s (%.2f years). Binding constraints: %s"
                % (self.window_start.date(), self.window_end.date(), self.usable_years,
                   ", ".join("%s/%s" % b for b in self.binding) or "none"))


def common_window(covs: Sequence[Coverage],
                  pairs: Sequence[str] = PAIRS,
                  timeframes: Sequence[str] = TIMEFRAMES) -> AdmissionReport:
    """Earliest common start = the LATEST of all earliest dates.

    Equivalently: the window opens only once every pair/timeframe exists. The newest
    series governs, which is exactly the point of the gate.
    """
    index: Dict[Tuple[str, str], Coverage] = {(c.pair, c.timeframe): c for c in covs}
    missing = [(p, tf) for p in pairs for tf in timeframes
               if (p, tf) not in index or not index[(p, tf)].present]
    if missing:
        return AdmissionReport(None, None, 0.0, False, missing=missing,
                               notes=["no overlapping window while series are absent"])

    starts = {k: index[k].earliest for k in index if k[0] in pairs and k[1] in timeframes}
    ends = {k: index[k].latest for k in index if k[0] in pairs and k[1] in timeframes}
    start = max(starts.values())
    end = min(ends.values())
    if start >= end:
        return AdmissionReport(start, end, 0.0, False,
                               notes=["series exist but do not overlap in time"])

    binding = sorted([k for k, v in starts.items() if v == start])
    years = (end - start).days / 365.25
    return AdmissionReport(start, end, years, True, binding=binding)


def admit_for_walk_forward(report: AdmissionReport, train_years: int = 4,
                           test_years: int = 1, min_folds: int = 3) -> AdmissionReport:
    """Require enough years for at least `min_folds` rolling folds.

    Needed length = train_years + min_folds * test_years - (min_folds - 1) * test_years
    which simplifies to train_years + test_years * 1... but folds slide by test_years,
    so the honest requirement is train_years + min_folds * test_years - test_years.
    """
    needed = train_years + test_years * min_folds
    if not report.admitted:
        return report
    if report.usable_years < needed:
        report.admitted = False
        report.notes.append(
            "usable %.2f years < %.2f required for %d folds of train=%dy/test=%dy"
            % (report.usable_years, needed, min_folds, train_years, test_years))
    else:
        report.notes.append(
            "usable %.2f years supports >= %d folds (train=%dy, test=%dy)"
            % (report.usable_years,
               int((report.usable_years - train_years) // test_years),
               train_years, test_years))
    return report
