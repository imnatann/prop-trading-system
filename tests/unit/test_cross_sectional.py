"""Tests for symbol-aware pip handling and the cross-sectional harness.

The pip tests exist because a fixed pip=0.0001 silently produced impossible
trades on JPY pairs -- 9 trades instead of 499, and a balance that rose from
10,000 to 22,000 in four trades. Nothing raised. Only a sanity test catches
that class of bug.
"""
from __future__ import annotations

import pytest

from config.fundingpips_rules import TWO_STEP_STANDARD
from research.backtest.cross_sectional import (
    MIN_POSITIVE_PAIRS,
    PROTOCOL_UNIVERSE,
    CrossSectionReport,
    PairResult,
    cross_sectional_walk_forward,
    evaluate_pair,
    load_universe,
)
from research.backtest.walkforward_real import StrategyParams, run_fold, walk_forward
from research.data.real_feed import (
    PIP_JPY,
    PIP_STANDARD,
    JPY_QUOTED,
    load_csv,
    pip_size_for,
    pip_value_per_lot_for,
)

import pathlib

CACHE = pathlib.Path("data/real")
HAVE_ALL = all((CACHE / ("%s_1d.csv" % s)).exists()
               for s in ("EURUSD", "USDJPY", "GBPUSD"))


# ============================================================= pip sizes

def test_standard_pairs_use_a_ten_thousandth():
    for sym in ("EURUSD", "GBPUSD", "AUDUSD", "USDCAD", "USDCHF"):
        assert pip_size_for(sym) == PIP_STANDARD


def test_jpy_pairs_use_one_hundredth():
    for sym in ("USDJPY", "EURJPY", "GBPJPY"):
        assert pip_size_for(sym) == PIP_JPY


def test_pip_size_tolerates_underscored_names():
    assert pip_size_for("USD_JPY") == PIP_JPY
    assert pip_size_for("EUR_USD") == PIP_STANDARD


def test_jpy_quoted_list_is_declared():
    assert "USDJPY" in JPY_QUOTED


# ======================================================= pip VALUE

def test_usd_quoted_pip_value_needs_no_conversion():
    """EURUSD: 0.0001 x 100,000 = 10.00 USD per pip per lot."""
    assert pip_value_per_lot_for("EURUSD") == pytest.approx(10.0)


def test_jpy_pip_value_requires_a_rate():
    """A silent 1.0 here overstated positions by ~149x. It must raise."""
    with pytest.raises(ValueError):
        pip_value_per_lot_for("USDJPY")


def test_jpy_pip_value_converts_correctly():
    """0.01 x 100,000 = 1,000 JPY; at 148.8 that is 6.72 USD."""
    got = pip_value_per_lot_for("USDJPY", jpy_rate=148.8)
    assert got == pytest.approx(1000.0 / 148.8)
    assert got < 10.0, "a JPY pip must be worth LESS than a USD-quoted pip"


def test_jpy_pip_value_rejects_a_nonpositive_rate():
    with pytest.raises(ValueError):
        pip_value_per_lot_for("USDJPY", jpy_rate=0.0)


# =============================================== the harness uses them

@pytest.mark.skipif(not HAVE_ALL, reason="no cached real data")
def test_usdjpy_produces_a_realistic_number_of_trades():
    """The bug produced 9 trades. Correct handling produces hundreds."""
    c = load_csv(CACHE / "USDJPY_1d.csv")
    r = walk_forward(c, StrategyParams(), TWO_STEP_STANDARD, symbol="USDJPY")
    n = sum(len(f.trade_records) for f in r.folds)
    assert n > 100, "USDJPY produced only %d trades - pip handling is wrong" % n


@pytest.mark.skipif(not HAVE_ALL, reason="no cached real data")
def test_usdjpy_balances_stay_physically_plausible():
    """A 0.1-lot book on a 10k account cannot double in four trades.

    This is the assertion that would have caught the ~149x overstatement.
    """
    c = load_csv(CACHE / "USDJPY_1d.csv")
    r = walk_forward(c, StrategyParams(), TWO_STEP_STANDARD, symbol="USDJPY")
    for f in r.folds:
        assert 5000.0 < f.final_balance < 30000.0, (
            "fold %d ended at %.0f - implausible for a 10k account at 0.1 lots"
            % (f.fold, f.final_balance))


@pytest.mark.skipif(not HAVE_ALL, reason="no cached real data")
def test_eurusd_is_unaffected_by_the_jpy_fix():
    """The fix must change JPY only, not regress the USD-quoted pairs."""
    c = load_csv(CACHE / "EURUSD_1d.csv")
    r = walk_forward(c, StrategyParams(), TWO_STEP_STANDARD, symbol="EURUSD")
    n = sum(len(f.trade_records) for f in r.folds)
    assert n == 299, "EURUSD trade count changed: %d" % n


# ==================================================== cross-section

def test_protocol_universe_is_six_pairs():
    assert len(PROTOCOL_UNIVERSE) == 6
    assert set(PROTOCOL_UNIVERSE) == {"EURUSD", "GBPUSD", "AUDUSD",
                                      "USDJPY", "USDCAD", "USDCHF"}


def test_min_positive_pairs_matches_protocol():
    """Protocol section 6: at least 4 of 6 pairs must be positive."""
    assert MIN_POSITIVE_PAIRS == 4


def test_missing_pairs_are_reported_not_skipped():
    data, missing = load_universe("data/definitely-not-here")
    assert data == {}
    assert len(missing) == 6


def test_verdict_requires_BOTH_breadth_and_survival():
    """A single survivor among six is luck, not a finding."""
    rep = CrossSectionReport(params={})
    rep.pairs = [
        PairResult(symbol="USDJPY", total_net=100.0, falsification="SURVIVED"),
        PairResult(symbol="EURUSD", total_net=-100.0, falsification="FALSIFIED"),
        PairResult(symbol="GBPUSD", total_net=-100.0, falsification="FALSIFIED"),
        PairResult(symbol="AUDUSD", total_net=-100.0, falsification="FALSIFIED"),
        PairResult(symbol="USDCAD", total_net=-100.0, falsification="FALSIFIED"),
        PairResult(symbol="USDCHF", total_net=-100.0, falsification="FALSIFIED"),
    ]
    assert rep.positive_pairs == 1
    assert rep.breadth_passes is False
    assert rep.any_survived_falsification is True
    assert rep.verdict() == "REJECTED"


def test_verdict_requires_breadth_even_with_no_survivor():
    rep = CrossSectionReport(params={})
    rep.pairs = [PairResult(symbol="P%d" % i, total_net=10.0,
                            falsification="FALSIFIED") for i in range(4)]
    assert rep.breadth_passes is True
    assert rep.any_survived_falsification is False
    assert rep.verdict() == "REJECTED"


def test_candidate_requires_both_conditions():
    rep = CrossSectionReport(params={})
    rep.pairs = [PairResult(symbol="P%d" % i, total_net=10.0,
                            falsification="SURVIVED") for i in range(4)]
    assert rep.verdict() == "CANDIDATE"


def test_empty_report_is_not_a_candidate():
    assert CrossSectionReport(params={}).verdict() == "NO DATA"


def test_summary_is_json_safe():
    import json
    rep = CrossSectionReport(params={"fast": 20})
    rep.pairs = [PairResult(symbol="EURUSD", total_net=1.0)]
    json.dumps(rep.summary())


@pytest.mark.skipif(not HAVE_ALL, reason="no cached real data")
def test_real_cross_section_is_currently_rejected():
    """Regression guard on the ACTUAL finding.

    The default parameter set reaches only 3 of 6 pairs positive, which is below
    the protocol's requirement of 4. If this ever flips, a human must look.
    """
    rep = cross_sectional_walk_forward()
    assert len(rep.pairs) == 6
    assert rep.positive_pairs == 3
    assert rep.breadth_passes is False
    assert rep.verdict() == "REJECTED"


@pytest.mark.skipif(not HAVE_ALL, reason="no cached real data")
def test_cross_sectional_uses_identical_params_for_every_pair():
    """Per-pair tuning would produce six meaningless optima."""
    rep = cross_sectional_walk_forward()
    assert rep.params == StrategyParams().as_dict()


def test_cross_sectional_module_imports_no_execution():
    import inspect
    import research.backtest.cross_sectional as m
    src = inspect.getsource(m)
    for forbidden in ("MetaTrader5", "fundingpips_mt5", "place_order"):
        assert forbidden not in src, forbidden
