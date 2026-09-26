"""Deterministic fold plan, frozen BEFORE any performance is read.

The plan is a pure function of the admitted window and fixed constants. It does not
consult returns, volatility, or "which years looked good". Two runs on the same
window produce byte-identical folds.

Layout:

    |---- TRAIN (train_years) ----|--purge--|--embargo--|-- TEST (test_years) --|
                                   step = test_years
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

import pandas as pd

from research.protocol.frozen_config import Frozen, FrozenViolation, TrainingWindow


@dataclass(frozen=True)
class Fold:
    index: int
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    purge_end: pd.Timestamp
    embargo_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp

    def train_window(self) -> TrainingWindow:
        return TrainingWindow(self.train_start, self.train_end, 0)

    def slice_train(self, df: pd.DataFrame) -> pd.DataFrame:
        return df.loc[(df.index >= self.train_start) & (df.index <= self.train_end)]

    def slice_test(self, df: pd.DataFrame) -> pd.DataFrame:
        return df.loc[(df.index >= self.test_start) & (df.index <= self.test_end)]

    def contains_in_gap(self, ts: pd.Timestamp) -> bool:
        """True if ts falls in purge or embargo - it must be in NEITHER set."""
        t = pd.Timestamp(ts)
        return (self.train_end < t <= self.embargo_end)


@dataclass
class FoldPlan(Frozen):
    """A frozen, ordered list of folds."""

    folds: List[Fold] = field(default_factory=list)
    spec: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        Frozen.__init__(self)

    def freeze(self, **spec: Any) -> None:
        self._freeze(TrainingWindow(self.folds[0].train_start,
                                    self.folds[-1].test_end,
                                    len(self.folds)), **spec)

    def fingerprint(self) -> str:
        blob = json.dumps([asdict(f) for f in self.folds], sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:32]


def build_fold_plan(window_start, window_end, train_years: int = 4,
                    test_years: int = 1, purge_days: int = 5,
                    embargo_days: int = 5) -> FoldPlan:
    """Deterministic rolling folds. Same inputs -> same folds, always."""
    s = pd.Timestamp(window_start, tz="UTC")
    e = pd.Timestamp(window_end, tz="UTC")
    if s >= e:
        raise ValueError("window start must precede end")
    folds: List[Fold] = []
    i = 0
    train_start = s
    while True:
        train_end = train_start + pd.DateOffset(years=train_years) - pd.Timedelta(days=1)
        purge_end = train_end + pd.Timedelta(days=purge_days)
        embargo_end = purge_end + pd.Timedelta(days=embargo_days)
        test_start = embargo_end + pd.Timedelta(days=1)
        test_end = test_start + pd.DateOffset(years=test_years) - pd.Timedelta(days=1)
        if test_end > e:
            break
        folds.append(Fold(index=i, train_start=train_start, train_end=train_end,
                          purge_end=purge_end, embargo_end=embargo_end,
                          test_start=test_start, test_end=test_end))
        i += 1
        train_start = train_start + pd.DateOffset(years=test_years)
    plan = FoldPlan(folds=folds)
    plan.freeze(train_years=train_years, test_years=test_years,
                purge_days=purge_days, embargo_days=embargo_days,
                window_start=str(s), window_end=str(e))
    return plan


def assert_no_leakage(plan: FoldPlan) -> None:
    """Structural invariants. Raises on any overlap or ordering violation."""
    if not plan.folds:
        raise FrozenViolation("fold plan is empty")
    for f in plan.folds:
        if not (f.train_start < f.train_end < f.purge_end < f.embargo_end
                < f.test_start < f.test_end):
            raise FrozenViolation("fold %d timestamps are not strictly ordered" % f.index)
        if f.train_end >= f.test_start:
            raise FrozenViolation("fold %d train overlaps test" % f.index)
    for a, b in zip(plan.folds, plan.folds[1:]):
        # NOTE: overlapping TRAIN windows are the defining property of a rolling
        # walk-forward and are expected. What must never overlap is the TEST sets,
        # and no fold may ever train on data that a LATER fold tests on at the same
        # time. The strict requirement is therefore on test periods and on the
        # train/test cut within each fold (checked above).
        if b.test_start <= a.test_end:
            raise FrozenViolation("folds %d and %d have overlapping test periods"
                                  % (a.index, b.index))
        if b.index != a.index + 1:
            raise FrozenViolation("fold indices are not contiguous")
    # test sets must be strictly increasing and disjoint
    for a, b in zip(plan.folds, plan.folds[1:]):
        if b.test_start <= a.test_end:
            raise FrozenViolation("test periods are not strictly increasing")


def verify_gap_excluded(plan: FoldPlan, df: pd.DataFrame) -> None:
    """No row in the purge/embargo gap may appear in train or test for that fold."""
    for f in plan.folds:
        tr = f.slice_train(df)
        te = f.slice_test(df)
        if len(tr) and len(te):
            if tr.index.max() >= te.index.min():
                raise FrozenViolation("fold %d: train max >= test min" % f.index)
        for ts in df.index:
            if f.contains_in_gap(ts):
                if ts in tr.index or ts in te.index:
                    raise FrozenViolation(
                        "fold %d: gap row %s leaked into train or test" % (f.index, ts))
