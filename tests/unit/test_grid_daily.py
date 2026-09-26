"""Tests for the pre-declared daily grid.

These tests exist to keep the grid HONEST: fixed size, all configs reported,
and a luck hurdle that grows with the number of attempts.
"""
from __future__ import annotations

import pathlib

import pytest

from research.backtest.grid_daily import (
    DECLARED_DAILY_GRID,
    LUCK_HURDLE_FLOOR,
    ConfigResult,
    GridReport,
    expected_max_sharpe_from_luck,
    run_grid,
)

CACHE = pathlib.Path("data/real")
HAVE_DATA = all((CACHE / ("%s_1d.csv" % s)).exists()
                for s in ("EURUSD", "GBPUSD", "AUDUSD", "USDJPY",
                          "USDCAD", "USDCHF"))


# ====================================================== the declared grid

def test_grid_is_small_and_fixed():
    """A large grid is how overfitting is manufactured, not how edge is found."""
    assert len(DECLARED_DAILY_GRID) == 4


def test_every_grid_entry_declares_the_same_knobs():
    keys = {frozenset(cfg.keys()) for cfg in DECLARED_DAILY_GRID}
    assert len(keys) == 1, "all configs must declare identical knobs"
    assert keys.pop() == {"fast", "slow", "stop_pips", "target_pips"}


def test_fast_is_always_shorter_than_slow():
    for cfg in DECLARED_DAILY_GRID:
        assert cfg["fast"] < cfg["slow"], cfg


def test_target_exceeds_stop():
    """Otherwise the reward:risk is inverted by accident."""
    for cfg in DECLARED_DAILY_GRID:
        assert cfg["target_pips"] > cfg["stop_pips"], cfg


# ======================================================== the luck hurdle

def test_single_config_has_only_the_floor_hurdle():
    assert expected_max_sharpe_from_luck(1) == LUCK_HURDLE_FLOOR


def test_hurdle_rises_with_the_number_of_attempts():
    """More attempts means the best one looks better by pure chance."""
    hurdles = [expected_max_sharpe_from_luck(n) for n in (1, 4, 10, 35)]
    for a, b in zip(hurdles, hurdles[1:]):
        assert b >= a, hurdles


def test_hurdle_matches_the_documented_table_shape():
    """Protocol section 6 lists ~0.50 at 10 years and ~1.11 at 2 years for N=4.

    Our approximation should at least be in the right ballpark and must never
    be LOWER than the floor.
    """
    got = expected_max_sharpe_from_luck(4, years=2)
    assert 0.5 <= got <= 1.6, got


def test_hurdle_falls_as_the_sample_grows():
    short = expected_max_sharpe_from_luck(4, years=2)
    long = expected_max_sharpe_from_luck(4, years=20)
    assert long <= short


def test_hurdle_never_returns_a_negative_or_zero_value():
    for n in (0, 1, 2, 100):
        assert expected_max_sharpe_from_luck(n) > 0


# ======================================================== report logic

def test_result_serialises_cleanly():
    import json
    r = ConfigResult(label="A", params={"fast": 20}, pairs_positive=4,
                     pairs_total=6, total_net=100.0, expectancy_pips=1.0,
                     survived_falsification=2, breadth_passes=True,
                     verdict="CANDIDATE")
    json.dumps(r.to_dict())


def test_best_is_chosen_by_breadth_then_net():
    rep = GridReport(results=[
        ConfigResult("A", {}, 4, 6, 100.0, 1.0, 0, True, "REJECTED"),
        ConfigResult("B", {}, 4, 6, 900.0, 1.0, 2, True, "CANDIDATE"),
        ConfigResult("C", {}, 2, 6, 9999.0, 1.0, 0, False, "REJECTED"),
    ], luck_hurdle=0.68)
    assert rep.best.label == "B"


def test_any_candidate_requires_the_CANDIDATE_verdict():
    rep = GridReport(results=[
        ConfigResult("A", {}, 4, 6, 100.0, 1.0, 0, True, "REJECTED")],
        luck_hurdle=0.68)
    assert rep.any_candidate is False

    rep2 = GridReport(results=[
        ConfigResult("A", {}, 4, 6, 100.0, 1.0, 3, True, "CANDIDATE")],
        luck_hurdle=0.68)
    assert rep2.any_candidate is True


def test_empty_report_has_no_best():
    assert GridReport(results=[], luck_hurdle=0.5).best is None


# ======================================================== real run

@pytest.mark.skipif(not HAVE_DATA, reason="no cached real data")
def test_grid_evaluates_every_declared_config():
    rep = run_grid()
    assert rep.n_configs == len(DECLARED_DAILY_GRID)
    for r in rep.results:
        assert r.pairs_total == 6


@pytest.mark.skipif(not HAVE_DATA, reason="no cached real data")
def test_real_grid_finds_no_candidate():
    """Regression guard on the ACTUAL finding.

    Two configs reach the 4/6 breadth threshold, but none survive
    falsification, so no configuration is a candidate. If this flips, a human
    must look before believing it.
    """
    rep = run_grid()
    assert rep.any_candidate is False
    breadth_reached = sum(1 for r in rep.results if r.breadth_passes)
    assert breadth_reached == 2
    assert all(r.survived_falsification <= 1 for r in rep.results)


@pytest.mark.skipif(not HAVE_DATA, reason="no cached real data")
def test_profit_is_outlier_driven_on_the_best_config():
    """The decisive failure: removing the top decile kills EVERY pair.

    This is the reason the grid verdict is REJECTED despite breadth passing.
    """
    import pandas as pd

    from config.fundingpips_rules import TWO_STEP_STANDARD
    from research.backtest.walkforward_real import (
        StrategyParams, build_folds, run_fold,
    )
    from research.data.real_feed import load_csv

    cfg = {"fast": 10, "slow": 50, "stop_pips": 40.0, "target_pips": 80.0}
    for sym in ("GBPUSD", "USDJPY", "USDCHF"):
        candles = load_csv(CACHE / ("%s_1d.csv" % sym))
        frames = []
        for f in build_folds(candles):
            o = run_fold(f.test(candles), StrategyParams(**cfg),
                         TWO_STEP_STANDARD, 10000.0, 1.0, 0.3,
                         fold_index=f.index, symbol=sym)
            if o.trade_records:
                frames.append(o.trades_frame())
        assert frames, sym
        df = pd.concat(frames, ignore_index=True)
        k = max(1, int(round(len(df) * 0.10)))
        without = df.drop(df["net_pnl"].nlargest(k).index)["net_pnl"].sum()
        assert without < 0, (
            "%s stayed positive without its top decile (%.0f) - the finding "
            "changed and the verdict must be revisited" % (sym, without))


def test_grid_module_imports_no_execution():
    import inspect
    import research.backtest.grid_daily as m
    src = inspect.getsource(m)
    for forbidden in ("MetaTrader5", "fundingpips_mt5", "place_order"):
        assert forbidden not in src, forbidden
