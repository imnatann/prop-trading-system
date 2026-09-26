"""Cross-sectional walk-forward across the protocol's 6-pair universe.

Why this matters more than a single-pair result
-----------------------------------------------
Protocol section 3 is explicit: the purpose of the universe is NOT more trades,
it is to distinguish

    "this is an FX phenomenon"      from      "this was EURUSD in one period"

A pattern of +0.8, +0.6, +0.5, +0.4, +0.3, +0.2 across six pairs is interesting.
A pattern of +4.2, -0.5, -0.3 across five flat pairs is not.

The independence caveat is enforced here rather than merely noted: these six
pairs share USD exposure, so six positive results are NOT six independent
observations. The report therefore leads with the BREAKTHROUGH COUNT (how many
pairs were positive) and never pools trades into one inflated sample size.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from research.backtest.falsify_real import falsify_trades
from research.backtest.walkforward_real import StrategyParams, walk_forward
from research.data.real_feed import load_csv
from config.fundingpips_rules import DEFAULT_MODEL, EvaluationModel

#: The pre-registered universe. Not adjustable after seeing results.
PROTOCOL_UNIVERSE = ("EURUSD", "GBPUSD", "AUDUSD", "USDJPY", "USDCAD", "USDCHF")

#: Protocol section 6: at least 4 of 6 pairs must be positive.
MIN_POSITIVE_PAIRS = 4


@dataclass
class PairResult:
    symbol: str
    folds: int = 0
    passed_folds: int = 0
    survived_folds: int = 0
    total_net: float = 0.0
    expectancy_pips: float = 0.0
    profit_factor: float = 0.0
    win_rate: float = 0.0
    falsification: str = "UNDERPOWERED"
    falsified_checks: List[str] = field(default_factory=list)

    @property
    def positive(self) -> bool:
        return self.total_net > 0

    def to_dict(self) -> Dict[str, object]:
        return {
            "symbol": self.symbol, "folds": self.folds,
            "passed_folds": self.passed_folds,
            "survived_folds": self.survived_folds,
            "total_net": round(self.total_net, 2),
            "expectancy_pips": round(self.expectancy_pips, 4),
            "profit_factor": round(self.profit_factor, 4),
            "win_rate": round(self.win_rate, 4),
            "positive": self.positive,
            "falsification": self.falsification,
            "falsified_checks": list(self.falsified_checks),
        }


@dataclass
class CrossSectionReport:
    params: Dict[str, float]
    pairs: List[PairResult] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)

    @property
    def positive_pairs(self) -> int:
        return sum(1 for p in self.pairs if p.positive)

    @property
    def breadth_passes(self) -> bool:
        return self.positive_pairs >= MIN_POSITIVE_PAIRS

    @property
    def any_survived_falsification(self) -> bool:
        return any(p.falsification == "SURVIVED" for p in self.pairs)

    def verdict(self) -> str:
        """A deliberately harsh combined verdict.

        Surviving falsification on ONE pair while five are negative is not a
        finding; it is the definition of a fluke. Both conditions are required.
        """
        if not self.pairs:
            return "NO DATA"
        if self.breadth_passes and self.any_survived_falsification:
            return "CANDIDATE"
        return "REJECTED"

    def summary(self) -> Dict[str, object]:
        return {
            "params": self.params,
            "pairs_evaluated": len(self.pairs),
            "pairs_missing": list(self.missing),
            "positive_pairs": self.positive_pairs,
            "required_positive_pairs": MIN_POSITIVE_PAIRS,
            "breadth_passes": self.breadth_passes,
            "any_survived_falsification": self.any_survived_falsification,
            "verdict": self.verdict(),
            "detail": [p.to_dict() for p in self.pairs],
        }


def load_universe(data_dir: str = "data/real",
                  universe=PROTOCOL_UNIVERSE):
    """Load cached CSVs for the universe. Missing pairs are reported, not skipped
    silently, because a partial universe silently weakens the breadth test."""
    d = Path(data_dir)
    out = {}
    missing = []
    for sym in universe:
        p = d / ("%s_1d.csv" % sym)
        if p.exists():
            out[sym] = load_csv(p)
        else:
            missing.append(sym)
    return out, missing


def evaluate_pair(symbol: str, candles, params: StrategyParams,
                  model: EvaluationModel = DEFAULT_MODEL,
                  initial_balance: float = 10000.0,
                  spread_pips: float = 1.0, slippage_pips: float = 0.3,
                  **kw) -> PairResult:
    """Walk-forward AND falsify one pair, returning both views."""
    res = walk_forward(candles, params, model, initial_balance=initial_balance,
                       spread_pips=spread_pips, slippage_pips=slippage_pips,
                       symbol=symbol, **kw)

    import pandas as pd
    frames = [f.trades_frame() for f in res.folds if len(f.trade_records)]
    if frames:
        combined = pd.concat(frames, ignore_index=True)
    else:
        combined = None

    f_report = falsify_trades(combined)
    return PairResult(
        symbol=symbol,
        folds=len(res.folds),
        passed_folds=sum(1 for f in res.folds if f.passed),
        survived_folds=sum(1 for f in res.folds if f.survived),
        total_net=float(f_report.metrics.get("total_net", 0.0)),
        expectancy_pips=float(f_report.metrics.get("expectancy_pips", 0.0)),
        profit_factor=float(f_report.metrics.get("profit_factor", 0.0)),
        win_rate=float(f_report.metrics.get("win_rate", 0.0)),
        falsification=f_report.verdict,
        falsified_checks=[k for k, ok in f_report.checks.items() if not ok],
    )


def cross_sectional_walk_forward(params: Optional[StrategyParams] = None,
                                 model: EvaluationModel = DEFAULT_MODEL,
                                 initial_balance: float = 10000.0,
                                 spread_pips: float = 1.0,
                                 slippage_pips: float = 0.3,
                                 data_dir: str = "data/real",
                                 universe=PROTOCOL_UNIVERSE,
                                 **kw) -> CrossSectionReport:
    """Run the SAME frozen parameters across every pair in the universe.

    Parameters are identical on purpose. Tuning per pair would produce six
    individually-optimal, jointly-meaningless results.
    """
    params = params or StrategyParams()
    data, missing = load_universe(data_dir, universe)
    report = CrossSectionReport(params=params.as_dict(), missing=missing)
    for sym in universe:
        candles = data.get(sym)
        if not candles:
            continue
        report.pairs.append(evaluate_pair(
            sym, candles, params, model, initial_balance,
            spread_pips, slippage_pips, **kw))
    return report
