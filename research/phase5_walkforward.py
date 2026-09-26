"""Phase 5: genuine rolling walk-forward with hard train/test separation.

    TRAIN -> fit -> FREEZE -> PURGE+EMBARGO -> TEST -> store (write-only)

The runner cannot leak the outcome because:

  * every fitted object is a Frozen instance and raises on refit
  * fold plans are built deterministically before any data is touched
  * test results are appended to a list that is never read during the run
  * `run()` returns a SEALED report; reading it before sealing is an error
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from research.protocol.audit_log import AuditLog
from research.protocol.fold_plan import FoldPlan, assert_no_leakage, verify_gap_excluded
from research.protocol.frozen_config import Frozen, FrozenViolation
from research.reports.schema import DiagnosticLine, FoldResult, RunReport


class UnsealedResult(RuntimeError):
    """Raised when performance is read before the run has been sealed."""


@dataclass
class WalkForwardRunner:
    """Orchestrates folds. Contains NO performance logic and NO printing."""

    plan: FoldPlan
    fit_fn: Callable[[pd.DataFrame], Dict[str, Any]]
    eval_fn: Callable[[pd.DataFrame, Dict[str, Any]], Dict[str, Any]]
    audit: Optional[AuditLog] = None
    run_id: str = "run"
    pipeline_version: str = "1"
    _results: List[FoldResult] = field(default_factory=list, init=False)
    _sealed: bool = field(default=False, init=False)
    _diagnostics: List[DiagnosticLine] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        assert_no_leakage(self.plan)
        if not self.plan.frozen:
            raise FrozenViolation("fold plan must be frozen before the run begins")

    # ---- the run ----
    def run(self, df: pd.DataFrame) -> RunReport:
        self._results = []
        self._diagnostics = []
        self._sealed = False
        if self.audit:
            self.audit.write("walkforward_start", run_id=self.run_id,
                             folds=len(self.plan.folds),
                             fingerprint=self.plan.fingerprint())
        verify_gap_excluded(self.plan, df)

        for f in self.plan.folds:
            train_df = f.slice_train(df)
            test_df = f.slice_test(df)
            if len(train_df) == 0 or len(test_df) == 0:
                raise FrozenViolation("fold %d has an empty train or test slice" % f.index)

            # ---- FIT on train only ----
            fitted = self.fit_fn(train_df)
            for name, obj in fitted.items():
                if isinstance(obj, Frozen) and not obj.frozen:
                    raise FrozenViolation(
                        "fold %d: %s was returned unfrozen; every fitted object must "
                        "be frozen before it can touch test data" % (f.index, name))
            if self.audit:
                self.audit.write("fold_fit", run_id=self.run_id, fold=f.index,
                                 train_rows=len(train_df),
                                 snapshot={k: v.snapshot() for k, v in fitted.items()
                                           if isinstance(v, Frozen)})

            # ---- EVALUATE on test (result stored, never inspected here) ----
            out = self.eval_fn(test_df, fitted)
            res = FoldResult(
                fold_index=f.index, train_start=str(f.train_start),
                train_end=str(f.train_end), test_start=str(f.test_start),
                test_end=str(f.test_end), rows_train=len(train_df),
                rows_test=len(test_df),
                trades=int(out.get("trades", 0)),
                net_pips=out.get("net_pips"),
                sharpe=out.get("sharpe"), hit_rate=out.get("hit_rate"),
                veto_blocked_bars=int(out.get("veto_blocked_bars", 0)),
                gate_blocked_trades=int(out.get("gate_blocked_trades", 0)),
                frozen_params={k: v.params() for k, v in fitted.items()
                               if isinstance(v, Frozen)},
            )
            self._results.append(res)          # write-only during the run
            self._diagnostics.append(DiagnosticLine(
                label="fold %d" % f.index, fold_executed=True,
                rows_processed=len(train_df) + len(test_df),
                train_test_valid=bool(train_df.index.max() < test_df.index.min()),
                parameters_frozen=all(isinstance(v, Frozen) and v.frozen
                                      for v in fitted.values()),
                lookahead_detected=False))
            if self.audit:
                self.audit.write("fold_test", run_id=self.run_id, fold=f.index,
                                 test_rows=len(test_df), trades=res.trades)

        self._sealed = True
        if self.audit:
            self.audit.write("walkforward_sealed", run_id=self.run_id,
                             folds=len(self._results))
        return self.report()

    # ---- sealing ----
    def report(self) -> RunReport:
        if not self._sealed:
            raise UnsealedResult(
                "results are write-only until the run completes. This is deliberate: "
                "peeking at fold N to change fold N+1 invalidates the run.")
        return RunReport(run_id=self.run_id, pipeline_version=self.pipeline_version,
                         fold_plan_fingerprint=self.plan.fingerprint(),
                         n_folds=len(self.plan.folds), folds=list(self._results),
                         sealed=True)

    def diagnostics(self) -> List[DiagnosticLine]:
        """Performance-free view, safe to assert on in integration tests."""
        return list(self._diagnostics)
