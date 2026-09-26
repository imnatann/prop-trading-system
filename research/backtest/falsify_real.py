"""Falsification over REAL walk-forward trades.

Why this module exists
----------------------
research/phase6_falsification.py declares the stress suite and ships the pure
helpers (drop-best-year, bootstrap, breadth). Until now nothing fed it real
trades, because the walk-forward only kept a COUNT. The suite therefore sat
orphaned: fully written, fully tested, and connected to nothing.

This module is the connector. It takes the per-trade records a walk-forward now
produces and runs the declared suite against them.

What a PASS means here
----------------------
Nothing like "this will make money". A pass means the parameter set survived the
pre-declared attacks, so it has not yet been falsified. A FAIL is the useful
outcome, because it costs nothing and saves an account.

The suite is NOT adjustable after seeing results. declared_stress_suite() is
imported rather than redefined, so the fingerprint stays stable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from research.phase6_falsification import (
    FalsificationCriteria,
    bootstrap_positive_probability,
    declared_stress_suite,
    drop_top_decile,
    cross_pair_breadth,
    suite_fingerprint,
)

#: Minimum trades before any statistical claim is worth making at all.
MIN_TRADES_FOR_VERDICT = 30


@dataclass
class FalsificationReport:
    n_trades: int
    n_folds: int
    suite_fingerprint: str
    checks: Dict[str, bool] = field(default_factory=dict)
    metrics: Dict[str, Any] = field(default_factory=dict)
    verdict: str = "UNDERPOWERED"
    passed: bool = False

    def summary(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "passed": self.passed,
            "n_trades": self.n_trades,
            "n_folds": self.n_folds,
            "suite_fingerprint": self.suite_fingerprint,
            "checks": dict(self.checks),
            "metrics": self.metrics,
        }

    def render(self) -> str:
        lines = []
        for key, ok in sorted(self.checks.items()):
            lines.append("  [%s] %s" % ("PASS" if ok else "FAIL", key))
        for key, val in sorted(self.metrics.items()):
            lines.append("  %-26s %s" % (key, val))
        return "\n".join(lines)


def _expectancy(pnls: Sequence[float]) -> float:
    """Mean net P&L per trade, in pips.

    Deliberately simple: with fixed lots there is no R-normalisation to do, and
    inventing one would hide the fact that position size is constant.
    """
    vals = [p for p in pnls if p == p]
    if not vals:
        return float("nan")
    return sum(vals) / len(vals)


def _profit_factor(pnls: Sequence[float]) -> float:
    gains = sum(p for p in pnls if p > 0)
    losses = -sum(p for p in pnls if p < 0)
    if losses <= 0:
        return float("inf") if gains > 0 else float("nan")
    return gains / losses


def falsify_trades(trades,
                   per_pair: Optional[Dict[str, float]] = None,
                   cost_survival_multiple: float = 0.0,
                   neighbourhood_ok: Optional[bool] = None,
                   criteria: Optional[FalsificationCriteria] = None,
                   n_bootstrap: int = 2000) -> FalsificationReport:
    """Run the declared suite against recorded trades.

    trades is a pandas DataFrame with at least a net_pnl column and, for the
    subset attacks, an exit_time column. A MISSING input produces an ABSENT
    check rather than a passed one, because "we did not test it" and "it passed"
    must never look the same.
    """
    crit = criteria or FalsificationCriteria()
    import pandas as pd

    suite = declared_stress_suite()
    fingerprint = suite_fingerprint(suite)

    if trades is None or len(trades) == 0:
        return FalsificationReport(
            n_trades=0, n_folds=0, suite_fingerprint=fingerprint,
            checks={}, metrics={}, verdict="UNDERPOWERED", passed=False)

    if "net_pnl" not in trades.columns:
        raise ValueError("trades must carry a net_pnl column")

    pnls: List[float] = [float(v) for v in trades["net_pnl"].tolist()]
    n_folds = int(trades["fold"].nunique()) if "fold" in trades.columns else 0

    metrics: Dict[str, Any] = {
        "expectancy_pips": round(_expectancy(pnls), 4),
        "profit_factor": round(_profit_factor(pnls), 4),
        "win_rate": round(sum(1 for p in pnls if p > 0) / len(pnls), 4),
        "total_net": round(sum(pnls), 2),
    }
    checks: Dict[str, bool] = {}

    # 1. remove the best YEAR: if one year carries everything, there is no edge
    if "exit_time" in trades.columns:
        df = trades.copy()
        df["_year"] = pd.to_datetime(df["exit_time"]).dt.year
        yearly = df.groupby("_year")["net_pnl"].sum()
        if len(yearly) > 1:
            best_year = yearly.idxmax()
            without = df[df["_year"] != best_year]["net_pnl"]
            checks["remove_best_year_positive"] = bool(without.sum() > 0)
            metrics["best_year"] = int(best_year)
            metrics["net_without_best_year"] = round(float(without.sum()), 2)

    # 2. remove the best FOLD (the out-of-sample analogue of remove-best-pair)
    if "fold" in trades.columns and n_folds > 1:
        fold_totals = trades.groupby("fold")["net_pnl"].sum()
        best_fold = fold_totals.idxmax()
        without = trades[trades["fold"] != best_fold]["net_pnl"]
        checks["remove_best_fold_positive"] = bool(without.sum() > 0)
        metrics["best_fold"] = int(best_fold)
        metrics["net_without_best_fold"] = round(float(without.sum()), 2)

    # 3. remove the top decile: is the whole result a handful of outliers?
    trimmed = drop_top_decile(trades, pnl_col="net_pnl")
    checks["remove_top_decile_positive"] = bool(trimmed["net_pnl"].sum() > 0)
    metrics["net_without_top_decile"] = round(float(trimmed["net_pnl"].sum()), 2)

    # 4. bootstrap: is the mean positive by more than luck?
    p_boot = bootstrap_positive_probability(pnls, n=n_bootstrap)
    checks["bootstrap_positive"] = bool(p_boot >= crit.min_bootstrap_p_positive)
    metrics["bootstrap_p_positive"] = round(float(p_boot), 4)

    # 5. cross-pair breadth, only when more than one pair was actually traded
    if per_pair:
        breadth = cross_pair_breadth(per_pair, crit)
        checks["cross_pair_breadth"] = bool(breadth["passes"])
        metrics["pairs_positive"] = "%d/%d" % (breadth["positive"], breadth["total"])

    # 6. cost survival
    if cost_survival_multiple > 0:
        checks["cost_stress"] = bool(
            cost_survival_multiple >= crit.max_cost_multiple_survived)
        metrics["cost_survival_multiple"] = cost_survival_multiple

    # 7. parameter neighbourhood
    if neighbourhood_ok is not None:
        checks["param_neighbourhood"] = bool(neighbourhood_ok)

    # ---- the verdict ------------------------------------------------------
    if len(pnls) < MIN_TRADES_FOR_VERDICT:
        verdict = "UNDERPOWERED"
        passed = False
    else:
        passed = bool(checks) and all(checks.values())
        verdict = "SURVIVED" if passed else "FALSIFIED"

    return FalsificationReport(
        n_trades=len(pnls), n_folds=n_folds,
        suite_fingerprint=fingerprint, checks=checks,
        metrics=metrics, verdict=verdict, passed=passed)


def falsify_walk_forward(result) -> FalsificationReport:
    """Convenience wrapper over a WalkForwardResult.

    Does NOT invent a cross-pair breadth number: the current strategy trades one
    symbol, so that check is absent rather than faked as passing.
    """
    import pandas as pd

    frames = []
    for f in getattr(result, "folds", []):
        df = f.trades_frame()
        if len(df):
            frames.append(df)
    if not frames:
        return falsify_trades(None)
    combined = pd.concat(frames, ignore_index=True)
    return falsify_trades(combined)
