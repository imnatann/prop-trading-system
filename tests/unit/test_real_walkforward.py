"""Tests for real-data ingestion and the walk-forward harness.

No network calls: these tests use a synthetic bar series and, when present, the
cached CSV. A test that silently depends on an external API is a flaky test.
"""
from __future__ import annotations

import datetime as _dt
import math
from pathlib import Path

import pytest

from config.fundingpips_rules import TWO_STEP_STANDARD
from research.backtest.walkforward_real import (
    COST_STRESS,
    Fold,
    StrategyParams,
    build_folds,
    cost_stress,
    run_fold,
    signal_at,
    sma,
    walk_forward,
)
from research.data.real_feed import (
    Candle,
    INTERVAL_LIMITS,
    YAHOO_SYMBOLS,
    load_csv,
    quality_report,
)

UTC = _dt.timezone.utc
CACHE = Path("data/real/EURUSD_1d.csv")


def make_candles(n: int = 900, start: str = "2016-01-01", step: float = 0.0002) -> list:
    """A deterministic, gently trending series - no RNG, so failures reproduce."""
    out = []
    t = _dt.datetime.fromisoformat(start).replace(tzinfo=UTC)
    price = 1.10000
    for i in range(n):
        drift = step if (i // 120) % 2 == 0 else -step
        o = price
        c = price + drift
        h = max(o, c) + 0.0002
        l = min(o, c) - 0.0002
        out.append(Candle(timestamp=t, open=o, high=h, low=l, close=c))
        price = c
        t += _dt.timedelta(days=1)
    return out


# ============================================================== real feed

def test_yahoo_symbol_map_covers_the_protocol_universe():
    for pair in ("EURUSD", "GBPUSD", "AUDUSD", "USDJPY", "USDCAD", "USDCHF"):
        assert pair in YAHOO_SYMBOLS


def test_interval_limits_document_the_binding_constraint():
    """Multi-year M15 is NOT available; the module must say so out loud."""
    assert "15m" in INTERVAL_LIMITS
    assert "NOT sufficient" in INTERVAL_LIMITS["15m"]


def test_quality_report_accepts_a_clean_series():
    q = quality_report(make_candles(300))
    assert q["ok"] is True
    assert q["crossed_bars"] == 0
    assert q["problems"] == []


def test_quality_report_detects_crossed_bars():
    bad = make_candles(50)
    bad[10] = Candle(timestamp=bad[10].timestamp, open=1.1, high=1.0, low=1.2, close=1.1)
    q = quality_report(bad)
    assert q["ok"] is False
    assert q["crossed_bars"] == 1


def test_quality_report_detects_nonpositive_prices():
    bad = make_candles(50)
    bad[5] = Candle(timestamp=bad[5].timestamp, open=0.0, high=0.1, low=-0.1, close=0.0)
    q = quality_report(bad)
    assert q["ok"] is False


def test_quality_report_handles_empty_input():
    q = quality_report([])
    assert q["bars"] == 0
    assert q["ok"] is False


@pytest.mark.skipif(not CACHE.exists(), reason="no cached real data")
def test_cached_real_data_is_structurally_sound():
    candles = load_csv(CACHE)
    assert len(candles) > 2000
    q = quality_report(candles)
    assert q["ok"] is True, q["problems"]
    assert q["span_years"] >= 9.0


# ================================================================ folds

def test_folds_never_overlap_and_train_precedes_test():
    candles = make_candles(3000)
    folds = build_folds(candles, train_years=4, test_years=1, embargo_days=30)
    assert folds
    for f in folds:
        assert f.train_start < f.train_end < f.test_start < f.test_end


def test_embargo_creates_a_real_gap_between_train_and_test():
    candles = make_candles(3000)
    folds = build_folds(candles, embargo_days=30)
    f = folds[0]
    assert (f.test_start - f.train_end).days == 30


def test_train_and_test_slices_do_not_intersect():
    candles = make_candles(3000)
    f = build_folds(candles)[0]
    tr = {c.timestamp for c in f.train(candles)}
    te = {c.timestamp for c in f.test(candles)}
    assert not (tr & te)


def test_too_little_data_yields_no_folds():
    assert build_folds(make_candles(200)) == []


# ============================================================ strategy

def test_sma_needs_a_full_window():
    vals = [1.0, 2.0, 3.0]
    assert sma(vals, 5, 2) is None
    assert sma(vals, 3, 2) == pytest.approx(2.0)


def test_sma_does_not_look_ahead():
    """The value at i must depend only on bars <= i."""
    vals = list(range(1, 11))
    before = sma(vals, 3, 5)
    vals[9] = 99999          # change a FUTURE bar
    assert sma(vals, 3, 5) == before


def test_signal_is_none_until_both_windows_are_ready():
    p = StrategyParams(fast=5, slow=20)
    closes = [1.0] * 30
    assert signal_at(closes, 3, p) is None


def test_signal_goes_long_when_fast_is_above_slow():
    p = StrategyParams(fast=2, slow=5)
    closes = [1.0, 1.0, 1.0, 1.0, 1.0, 1.1, 1.2]
    assert signal_at(closes, 6, p) == "BUY"


def test_signal_goes_short_when_fast_is_below_slow():
    p = StrategyParams(fast=2, slow=5)
    closes = [1.0, 1.0, 1.0, 1.0, 1.0, 0.9, 0.8]
    assert signal_at(closes, 6, p) == "SELL"


# =========================================================== run_fold

def test_run_fold_on_empty_slice_is_safe():
    o = run_fold([], StrategyParams(), TWO_STEP_STANDARD, 10000.0, 1.0, 0.3)
    assert o.trades == 0
    assert o.survived is True


def test_run_fold_never_exceeds_a_breached_account():
    o = run_fold(make_candles(900), StrategyParams(), TWO_STEP_STANDARD,
                 10000.0, 1.0, 0.3)
    # A breach ends the evaluation; it must never report passed.
    if not o.survived:
        assert o.passed is False


def test_higher_costs_cannot_improve_the_result():
    """Costs are a drag. A monotonicity violation means a modelling bug."""
    candles = make_candles(900)
    finals = [run_fold(candles, StrategyParams(), TWO_STEP_STANDARD, 10000.0,
                       spread_pips=s, slippage_pips=s / 3.0).final_balance
              for s in (0.0, 2.0, 8.0, 20.0)]
    for earlier, later in zip(finals, finals[1:]):
        assert later <= earlier + 1e-6, finals


def test_larger_positions_amplify_both_directions():
    candles = make_candles(900)
    small = run_fold(candles, StrategyParams(lots=0.1), TWO_STEP_STANDARD,
                     10000.0, 1.0, 0.3).final_balance
    big = run_fold(candles, StrategyParams(lots=0.5), TWO_STEP_STANDARD,
                   10000.0, 1.0, 0.3).final_balance
    assert abs(big - 10000.0) > abs(small - 10000.0)


# ======================================================== walk-forward

def test_walk_forward_reports_one_outcome_per_fold():
    candles = make_candles(3000)
    r = walk_forward(candles, StrategyParams(), TWO_STEP_STANDARD)
    n_folds = len(build_folds(candles))
    assert len(r.folds) == n_folds
    assert 0.0 <= r.pass_rate <= 1.0
    assert 0.0 <= r.survival_rate <= 1.0


def test_pass_rate_can_never_exceed_survival_rate():
    """Passing requires surviving; the arithmetic must reflect that."""
    candles = make_candles(3000)
    for lots in (0.1, 0.3, 0.6):
        r = walk_forward(candles, StrategyParams(lots=lots), TWO_STEP_STANDARD)
        assert r.pass_rate <= r.survival_rate + 1e-12


def test_walk_forward_params_are_frozen_across_folds():
    r = walk_forward(make_candles(3000), StrategyParams(), TWO_STEP_STANDARD)
    expected = StrategyParams().as_dict()
    assert r.params == expected


def test_summary_is_json_safe():
    import json
    r = walk_forward(make_candles(3000), StrategyParams(), TWO_STEP_STANDARD)
    json.dumps(r.summary())          # must not raise


# ========================================================= cost stress

def test_cost_stress_covers_four_escalating_levels():
    assert len(COST_STRESS) == 4
    spreads = [s for _, s, _ in COST_STRESS]
    assert spreads == sorted(spreads)


def test_cost_stress_returns_a_row_per_level():
    rows = cost_stress(make_candles(3000), StrategyParams(), TWO_STEP_STANDARD)
    assert len(rows) == len(COST_STRESS)
    for r in rows:
        assert 0.0 <= r["survival_rate"] <= 1.0


def test_cost_stress_never_improves_survival_as_costs_rise():
    rows = cost_stress(make_candles(3000), StrategyParams(), TWO_STEP_STANDARD)
    rates = [r["survival_rate"] for r in rows]
    for earlier, later in zip(rates, rates[1:]):
        assert later <= earlier + 1e-12, rates


# ============================================================ boundaries

def test_harness_does_not_import_execution_or_mt5():
    import inspect
    import research.backtest.walkforward_real as m
    src = inspect.getsource(m)
    for forbidden in ("MetaTrader5", "fundingpips_mt5", "order_send",
                      "place_order", "zscore_scalper"):
        assert forbidden not in src, forbidden


def test_real_feed_does_not_import_execution():
    import inspect
    import research.data.real_feed as m
    src = inspect.getsource(m)
    for forbidden in ("MetaTrader5", "fundingpips_mt5", "place_order"):
        assert forbidden not in src, forbidden
