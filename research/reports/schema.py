"""Report schema. Deliberately storage-only: no formatting, no printing.

Reason: a runner that can print Sharpe mid-run invites the researcher to peek at
Fold 1 and change code before Fold 2. Results are accumulated into these objects and
only materialised when the whole run has finished.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class FoldResult:
    fold_index: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    rows_train: int
    rows_test: int
    trades: int = 0
    # performance fields are None until the run is sealed; see SealedResults
    net_pips: Optional[float] = None
    sharpe: Optional[float] = None
    hit_rate: Optional[float] = None
    veto_blocked_bars: int = 0
    gate_blocked_trades: int = 0
    frozen_params: Dict[str, Any] = field(default_factory=dict)
    audit: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RunReport:
    run_id: str
    pipeline_version: str
    fold_plan_fingerprint: str
    n_folds: int
    folds: List[FoldResult] = field(default_factory=list)
    verdict: Optional[str] = None
    notes: List[str] = field(default_factory=list)
    sealed: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DiagnosticLine:
    """The ONLY thing an integration test is allowed to assert on.

    Contains no performance metric by construction, so a test cannot be written that
    accidentally depends on results.
    """
    label: str
    fold_executed: bool
    rows_processed: int
    train_test_valid: bool
    parameters_frozen: bool
    lookahead_detected: bool

    def render(self) -> str:
        return ("%s: fold executed: %s | rows processed: %d | train/test valid: %s | "
                "parameter frozen: %s | look-ahead detected: %s"
                % (self.label, "yes" if self.fold_executed else "no",
                   self.rows_processed, "yes" if self.train_test_valid else "no",
                   "yes" if self.parameters_frozen else "no",
                   "yes" if self.lookahead_detected else "no"))
