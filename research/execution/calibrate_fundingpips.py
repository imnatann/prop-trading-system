"""
FundingPips execution calibration.

Turns MEASURED FundingPips conditions into inputs simulator_v2 can consume.

Discipline
----------
This module does NOT modify simulator_v2, and it does NOT bake FundingPips constants
into the simulator. It reads a FundingPipsExecutionProfile (captured from live MT5
metadata + spread telemetry) and emits a SimulationCostParameters data object. The
dependency arrow is execution -> research, never research -> execution.

It also produces the calibration REPORT: spread by UTC hour, spread by session, and
median/p75/p90/p95/p99. Those are execution-plumbing statistics. They are NOT
performance metrics: no Sharpe, P&L, profit factor, win rate, or strategy ranking is
computed anywhere in this module, by design.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from src.execution.execution_profile import (
    PROFILE_DIR,
    FundingPipsExecutionProfile,
    SpreadStats,
)
from src.execution.models import SimulationCostParameters
from src.execution.telemetry import SpreadRecorder, SpreadSample

CALIBRATION_DIR = PROFILE_DIR
CALIBRATION_REPORT_PATH = CALIBRATION_DIR / "calibration_report.json"

#: Spread quantiles reported. Chosen so a cost-stress run can select a tail value.
REPORTED_QUANTILES = ("p50", "p75", "p90", "p95", "p99")


def load_samples(path: Path) -> List[SpreadSample]:
    """Read recorded spread JSONL back into SpreadSample objects."""
    samples: List[SpreadSample] = []
    if not path.exists():
        return samples
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            samples.append(
                SpreadSample(
                    timestamp_utc=datetime.fromisoformat(payload["timestamp_utc"]),
                    canonical_symbol=payload["canonical_symbol"],
                    provider_symbol=payload.get("provider_symbol", ""),
                    bid=float(payload["bid"]),
                    ask=float(payload["ask"]),
                    point=float(payload.get("point", 0.00001) or 0.00001),
                    pip_size=float(payload.get("pip_size", 0.0001) or 0.0001),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return samples


def stats_from_samples(samples: Sequence[SpreadSample], symbol: str = "EURUSD") -> SpreadStats:
    """Build distribution statistics from recorded samples."""
    recorder = SpreadRecorder(symbol)
    for sample in samples:
        recorder.add(sample)
    return recorder.stats()


def build_calibration_report(
    profile: FundingPipsExecutionProfile,
    extra_symbols: Optional[Dict[str, SpreadStats]] = None,
) -> Dict[str, Any]:
    """Assemble the execution calibration report.

    Contains spread distribution statistics ONLY. No performance metric.
    """
    stats = profile.spread_stats or SpreadStats(samples=0)
    report: Dict[str, Any] = {
        "report_type": "execution_calibration",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "provider": "fundingpips_mt5",
        "server": profile.account.server if profile.account else None,
        "canonical_symbol": profile.canonical_symbol,
        "provider_symbol": profile.provider_symbol,
        "pip_size": profile.pip_size,
        "spread": {
            "samples": stats.samples,
            "p50": stats.p50,
            "p75": stats.p75,
            "p90": stats.p90,
            "p95": stats.p95,
            "p99": stats.p99,
            "mean": stats.mean,
            "min": stats.minimum,
            "max": stats.maximum,
            "by_hour_utc": {str(k): v for k, v in sorted(stats.by_hour_utc.items())},
            "by_session": dict(sorted(stats.by_session.items())),
        },
        "declared_spread_pips": profile.symbol.spread_pips,
        "swap_long_pips": profile.swap_long_pips,
        "swap_short_pips": profile.swap_short_pips,
        "fills_observed": len(profile.fills),
        "observed_slippage_pips": profile.observed_slippage_pips,
        "commission_per_lot_per_side": profile.commission_per_lot_per_side,
        "simulation_costs": {
            q: profile.to_simulation_costs(spread_quantile=q).to_dict()
            for q in REPORTED_QUANTILES
        },
        "notes": (
            "Execution calibration only. Contains no strategy performance metric. "
            "FundingPips telemetry is execution calibration data and is deliberately "
            "kept separate from any historical research data."
        ),
    }
    if extra_symbols:
        report["additional_symbols"] = {k: v.to_dict() for k, v in extra_symbols.items()}
    return report


def save_calibration_report(report: Dict[str, Any], path: Optional[Path] = None) -> Path:
    """Atomically persist the calibration report."""
    target = Path(path) if path else CALIBRATION_REPORT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(target)
    return target


def to_simulation_cost_parameters(
    profile: FundingPipsExecutionProfile,
    spread_quantile: str = "p50",
    spread_multiple: float = 1.0,
) -> SimulationCostParameters:
    """Convert a measured profile into simulator_v2 cost inputs.

    Args:
        spread_quantile: p50/p75/p90/p95/p99, selected from observed telemetry.
        spread_multiple: an explicit STRESS multiplier, applied only when the caller
            asks for it. 1.0 is the honest baseline; there is no hidden inflation.
    """
    params = profile.to_simulation_costs(spread_quantile=spread_quantile)
    if spread_multiple != 1.0:
        params.spread_pips = params.spread_pips * spread_multiple
        params.notes += ";spread_x%s" % spread_multiple
    return params

