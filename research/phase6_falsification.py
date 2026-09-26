"""Phase 6: falsification. Declared in advance, before any result exists.

Goal is to KILL the strategy, not to confirm it. Every stress case is defined here as
data, so that nobody can later say "let us also try EMA+3 because it seems to fit".
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class StressCase:
    """One pre-declared falsification test."""
    key: str
    description: str
    kind: str                       # cost | delay | perturb | subset | mcmc
    magnitude: float = 0.0
    param: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)


def declared_stress_suite() -> List[StressCase]:
    """THE pre-declared suite. Fixed now; adding a case later is a violation."""
    cases: List[StressCase] = [
        StressCase("cost_x1_0", "baseline cost", "cost", magnitude=1.0),
        StressCase("cost_x1_5", "spread/slippage/commission x1.5", "cost", magnitude=1.5),
        StressCase("cost_x2_0", "spread/slippage/commission x2.0", "cost", magnitude=2.0),
        StressCase("delay_1bar", "entry delayed by one bar", "delay", magnitude=1.0),
        StressCase("cost_x1_5_delay", "x1.5 cost AND 1-bar delay", "delay",
                   magnitude=1.0, meta={"cost": 1.5}),
    ]
    # parameter neighbourhood: +/-20 percent on the two frozen knobs, declared NOW
    for p in ("pullback_atr", "breakout_bars"):
        for rel in (-0.2, 0.2):
            cases.append(StressCase("perturb_%s_%+d" % (p, int(rel * 100)),
                                    "neighbour of %s" % p, "perturb",
                                    magnitude=rel, param=p))
    cases += [
        StressCase("remove_best_year", "drop the single best calendar year",
                   "subset", meta={"drop": "best_year"}),
        StressCase("remove_best_pair", "drop the single best pair",
                   "subset", meta={"drop": "best_pair"}),
        StressCase("remove_best_trade_decile", "drop the top 10 percent of trades",
                   "subset", meta={"drop": "top_decile"}),
        StressCase("subperiod_first_half", "first half of the OOS span",
                   "subset", meta={"half": "first"}),
        StressCase("subperiod_second_half", "second half of the OOS span",
                   "subset", meta={"half": "second"}),
        StressCase("bootstrap_trade_order", "bootstrap resample of trade order",
                   "mcmc", magnitude=2000.0),
    ]
    return cases


def suite_fingerprint(suite: Sequence[StressCase]) -> str:
    blob = "|".join(sorted("%s:%s:%s:%s" % (c.key, c.kind, c.magnitude, c.param)
                           for c in suite))
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


@dataclass(frozen=True)
class FalsificationCriteria:
    """Survival thresholds, fixed before results and not adjustable after."""
    min_pairs_positive: int = 4
    n_pairs: int = 6
    max_cost_multiple_survived: float = 2.0
    min_bootstrap_p_positive: float = 0.95
    require_param_neighbourhood: bool = True


def apply_perturbation(cfg: Dict[str, Any], case: StressCase) -> Dict[str, Any]:
    """Deterministic, declared parameter perturbation."""
    if case.kind != "perturb" or not case.param:
        return dict(cfg)
    out = dict(cfg)
    base = out.get(case.param)
    if base is None:
        return out
    if isinstance(base, int) and not isinstance(base, bool):
        out[case.param] = max(1, int(round(base * (1.0 + case.magnitude))))
    else:
        out[case.param] = float(base) * (1.0 + case.magnitude)
    return out


def delayed_entry_frame(df: pd.DataFrame, bars: int = 1) -> pd.DataFrame:
    """Shift execution forward. Signals unchanged; fills get worse."""
    out = df.copy()
    for c in ("open", "high", "low", "close"):
        if c in out.columns:
            out[c] = out[c].shift(-bars)
    return out.dropna(subset=["close"])


def drop_best_year(trades: pd.DataFrame, pnl_col: str = "net_pips") -> pd.DataFrame:
    if trades.empty or pnl_col not in trades.columns:
        return trades
    yr = pd.to_datetime(trades["exit_time"]).dt.year
    totals = trades.groupby(yr)[pnl_col].sum()
    if totals.empty:
        return trades
    return trades[yr != totals.idxmax()]


def drop_best_pair(trades: pd.DataFrame, pnl_col: str = "net_pips") -> pd.DataFrame:
    if trades.empty or "symbol" not in trades.columns:
        return trades
    totals = trades.groupby("symbol")[pnl_col].sum()
    if totals.empty:
        return trades
    return trades[trades["symbol"] != totals.idxmax()]


def drop_top_decile(trades: pd.DataFrame, pnl_col: str = "net_pips") -> pd.DataFrame:
    if trades.empty:
        return trades
    k = max(1, int(round(len(trades) * 0.10)))
    return trades.drop(index=trades[pnl_col].nlargest(k).index)


def bootstrap_positive_probability(pnls: Sequence[float], n: int = 2000,
                                   seed: int = 20260924) -> float:
    """Probability the mean trade P&L is positive under a bootstrap resample."""
    x = np.asarray([v for v in pnls if np.isfinite(v)], dtype=float)
    if len(x) < 10:
        return float("nan")
    rng = np.random.default_rng(seed)
    means = rng.choice(x, size=(n, len(x)), replace=True).mean(axis=1)
    return float((means > 0).mean())


def cross_pair_breadth(per_pair: Dict[str, float],
                       criteria: FalsificationCriteria) -> Dict[str, Any]:
    pos = [k for k, v in per_pair.items() if v > 0]
    return {"positive": len(pos), "total": len(per_pair),
            "passes": len(pos) >= criteria.min_pairs_positive,
            "detail": dict(sorted(per_pair.items()))}


def evaluate_falsification(results: Dict[str, Any],
                           criteria: Optional[FalsificationCriteria] = None) -> Dict[str, Any]:
    """Combine stress outcomes into one verdict. Pure function of the results."""
    c = criteria or FalsificationCriteria()
    checks: Dict[str, bool] = {}
    if "per_pair" in results:
        checks["cross_pair_breadth"] = bool(
            cross_pair_breadth(results["per_pair"], c)["passes"])
    if "cost_survival" in results:
        checks["cost_stress"] = bool(
            results["cost_survival"] >= c.max_cost_multiple_survived)
    if "bootstrap_p" in results:
        checks["bootstrap_positive"] = bool(
            results["bootstrap_p"] >= c.min_bootstrap_p_positive)
    if c.require_param_neighbourhood and "neighbourhood_ok" in results:
        checks["param_neighbourhood"] = bool(results["neighbourhood_ok"])
    return {"checks": checks, "passed": all(checks.values()) if checks else False,
            "n_checks": len(checks)}
