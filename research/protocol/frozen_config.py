"""Frozen objects: things that, once fitted on TRAIN, may never be refitted.

The single most dangerous failure mode in this project is silent recalibration on
test data. `Frozen` makes that a runtime error rather than a code-review question.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

import pandas as pd


class FrozenViolation(RuntimeError):
    """Raised when a frozen object is mutated or refitted."""


@dataclass
class TrainingWindow:
    """The exact data a frozen object was fitted on. Provenance, not decoration."""
    start: pd.Timestamp
    end: pd.Timestamp
    rows: int

    def contains(self, ts: pd.Timestamp) -> bool:
        return self.start <= pd.Timestamp(ts) <= self.end

    def overlaps(self, other: "TrainingWindow") -> bool:
        return not (self.end < other.start or other.end < self.start)


class Frozen:
    """Base class for anything fitted on train and consumed on test.

    Subclasses call `self._freeze()` at the end of `fit`. After that:
      * any attribute write raises FrozenViolation
      * `refit` raises FrozenViolation
    Consuming code calls `transform`/`allow`, which never mutate state.
    """

    def __init__(self) -> None:
        object.__setattr__(self, "_frozen", False)
        object.__setattr__(self, "_training_window", None)
        object.__setattr__(self, "_params", {})

    # ---- guards ----
    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_frozen", False) and not name.startswith("_"):
            raise FrozenViolation(
                "%s is frozen; refusing to set %r. Refit would leak test data into "
                "the model." % (type(self).__name__, name))
        object.__setattr__(self, name, value)

    @property
    def frozen(self) -> bool:
        return bool(getattr(self, "_frozen", False))

    @property
    def training_window(self) -> Optional[TrainingWindow]:
        return getattr(self, "_training_window", None)

    def params(self) -> Dict[str, Any]:
        return dict(getattr(self, "_params", {}))

    def _freeze(self, window: TrainingWindow, **params: Any) -> None:
        object.__setattr__(self, "_training_window", window)
        object.__setattr__(self, "_params", dict(params))
        object.__setattr__(self, "_frozen", True)

    def refit(self, *args: Any, **kwargs: Any) -> None:
        raise FrozenViolation(
            "%s.refit() is not permitted. A frozen object must be rebuilt from "
            "scratch on the new training window, never recalibrated in place."
            % type(self).__name__)

    def assert_frozen(self) -> None:
        if not self.frozen:
            raise FrozenViolation("%s was used before fit()" % type(self).__name__)

    def snapshot(self) -> Dict[str, Any]:
        """Serialisable description for the audit log."""
        tw = self.training_window
        return {
            "type": type(self).__name__,
            "frozen": self.frozen,
            "params": self.params(),
            "trained_on": (str(tw.start), str(tw.end)) if tw else None,
            "train_rows": tw.rows if tw else None,
        }


def window_from_index(idx: pd.Index) -> TrainingWindow:
    """Build a TrainingWindow from a dataframe index."""
    if len(idx) == 0:
        raise ValueError("cannot build a training window from an empty index")
    return TrainingWindow(start=pd.Timestamp(idx[0]), end=pd.Timestamp(idx[-1]),
                          rows=len(idx))
