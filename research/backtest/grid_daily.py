"""Pre-declared daily parameter grid, with the multiple-testing cost paid upfront.

Why this file exists
--------------------
The project's pre-registered grid (protocol section 2) is H4/H1/M15 multi-timeframe.
That grid CANNOT be tested on the available feed: Yahoo serves 10 years of daily
bars but only 60 days of M15, so zero walk-forward folds can be built at the
timeframe the hypothesis needs. Claiming to test it would be fabricating a result,
so this module does NOT do that.

What it does instead is apply the SAME discipline to the hypothesis the data can
actually support: a daily trend-following rule. That means:

  * the grid is DECLARED here, in code, before any result is seen
  * the number of configurations is SMALL and fixed
  * every configuration is reported, not just the winner
  * the luck hurdle is computed from the actual grid size and span

The last point is the one people skip. If you try N configurations and report the
best, the expected best Sharpe from pure luck RISES with N. This module makes that
cost explicit rather than leaving it implicit.

Read this before trusting any number it prints
----------------------------------------------
A grid search over real data is still a search. Several configurations WILL look
good. What separates a finding from a coincidence is whether the winner survives
out-of-sample AND whether the breadth is there across pairs.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from research.backtest.cross_sectional import (
    PROTOCOL_UNIVERSE,
    CrossSectionReport,
    cross_sectional_walk_forward,
)
from research.backtest.falsify_real import falsify_trades
from research.backtest.walkforward_real import StrategyParams, walk_forward
from research.data.real_feed import load_csv

#: The FULL search space. Declared before results; adding a row later invalidates
#: every number this module has produced.
DECLARED_DAILY_GRID: Sequence[Dict[str, float]] = (
    {"fast": 20, "slow": 100, "stop_pips": 60.0, "target_pips": 120.0},
    {"fast": 20, "slow": 200, "stop_pips": 60.0, "target_pips": 120.0},
    {"fast": 50, "slow": 200, "stop_pips": 60.0, "target_pips": 120.0},
    {"fast": 10, "slow": 50, "stop_pips": 40.0, "target_pips": 80.0},
)

#: The span, in years, that the luck hurdle is computed against. Taken from the
#: actual walk-forward test window, not assumed.
GRID_SPAN_YEARS = 6.0

#: Protocol section 6's hurdle table, extended by the standard
#: expected-maximum-of-N-normals approximation: E[max] ~= sqrt(2 ln N).
#: At N=1 the hurdle is ~0 by construction, so a floor is applied.
LUCK_HURDLE_FLOOR = 0.10


def expected_max_sharpe_from_luck(n_configs: int,
                                  years: float = GRID_SPAN_YEARS) -> float:
    """Rough Sharpe a pure-noise strategy reaches as the BEST of N attempts.

    Annualised: the expected maximum of N independent standard normals is about
    sqrt(2 ln N), and a Sharpe measured over T years has a standard error of
    about 1/sqrt(T). Multiplying gives the luck hurdle.

    This is an approximation, stated as one. It is used to set expectations, not
    to certify anything.
    """
    if n_configs <= 1:
        return LUCK_HURDLE_FLOOR
    e_max = math.sqrt(2.0 * math.log(n_configs))
    return max(LUCK_HURDLE_FLOOR, e_max / math.sqrt(max(years, 1e-9)))


@dataclass
class ConfigResult:
    label: str
    params: Dict[str, float]
    pairs_positive: int
    pairs_total: int
    total_net: float
    expectancy_pips: float
    survived_falsification: int
    breadth_passes: bool
    verdict: str

    def to_dict(self) -> Dict[str, object]:
        return {
            "label": self.label, "params": self.params,
            "pairs_positive": self.pairs_positive, "pairs_total": self.pairs_total,
            "total_net": round(self.total_net, 2),
            "expectancy_pips": round(self.expectancy_pips, 4),
            "survived_falsification": self.survived_falsification,
            "breadth_passes": self.breadth_passes,
            "verdict": self.verdict,
        }


@dataclass
class GridReport:
    results: List[ConfigResult]
    luck_hurdle: float

    @property
    def n_configs(self) -> int:
        return len(self.results)

    @property
    def best(self) -> Optional[ConfigResult]:
        """Best by pairs-positive, then by total net. Reported for transparency,
        NOT as a recommendation."""
        if not self.results:
            return None
        return max(self.results,
                   key=lambda r: (r.pairs_positive, r.total_net))

    @property
    def any_candidate(self) -> bool:
        return any(r.verdict == "CANDIDATE" for r in self.results)

    def summary(self) -> Dict[str, object]:
        b = self.best
        return {
            "n_configs": self.n_configs,
            "luck_hurdle_sharpe": round(self.luck_hurdle, 4),
            "best_label": b.label if b else None,
            "best_pairs_positive": b.pairs_positive if b else 0,
            "any_candidate": self.any_candidate,
            "results": [r.to_dict() for r in self.results],
        }


def run_grid(data_dir: str = "data/real",
             universe=PROTOCOL_UNIVERSE,
             grid: Sequence[Dict[str, float]] = DECLARED_DAILY_GRID,
             balance: float = 10000.0,
             spread: float = 1.0,
             slippage: float = 0.3,
             **kw) -> GridReport:
    """Evaluate EVERY declared configuration across the universe.

    All configurations are reported. Selecting and reporting only the winner is
    the exact failure mode the protocol was written to prevent.
    """
    results: List[ConfigResult] = []
    for i, cfg in enumerate(grid):
        params = StrategyParams(**cfg)
        rep: CrossSectionReport = cross_sectional_walk_forward(
            params=params, initial_balance=balance,
            spread_pips=spread, slippage_pips=slippage,
            data_dir=data_dir, universe=universe, **kw)
        s = rep.summary()
        results.append(ConfigResult(
            label=chr(ord("A") + i),
            params=params.as_dict(),
            pairs_positive=s["positive_pairs"],
            pairs_total=s["pairs_evaluated"],
            total_net=sum(d["total_net"] for d in s["detail"]),
            expectancy_pips=(sum(d["expectancy_pips"] for d in s["detail"])
                             / max(1, s["pairs_evaluated"])),
            survived_falsification=sum(
                1 for d in s["detail"] if d["falsification"] == "SURVIVED"),
            breadth_passes=s["breadth_passes"],
            verdict=s["verdict"],
        ))
    return GridReport(results=results,
                      luck_hurdle=expected_max_sharpe_from_luck(len(results)))
