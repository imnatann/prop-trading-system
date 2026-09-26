"""Tests for the Phase 6 falsification connector over real trades.

These tests exist because the falsification suite previously ran against
nothing. A test module that only checks the pure helpers would not have caught
that; these check the CONNECTION to per-trade records.
"""
from __future__ import annotations

import datetime as _dt

import pytest

from research.backtest.falsify_real import (
    MIN_TRADES_FOR_VERDICT,
    FalsificationReport,
    falsify_trades,
    falsify_walk_forward,
)
from research.backtest.walkforward_real import (
    FoldOutcome,
    StrategyParams,
    TradeRecord,
    WalkForwardResult,
    run_fold,
)
from research.data.real_feed import load_csv
from config.fundingpips_rules import TWO_STEP_STANDARD


def make_trades(n: int = 100, win_every: int = 3, win: float = 100.0,
                loss: float = -50.0, start_year: int = 2020,
                days_per_trade: int = 7):
    """A deterministic trade frame. No RNG, so failures reproduce.

    days_per_trade matters: the remove-best-YEAR attack is only DEFINED when
    the sample spans more than one calendar year. Defaulting to a weekly step
    means n=200 covers ~4 years, which is what makes that check present at all.
    A test that expected the check on a single-year sample was asserting a bug.
    """
    import pandas as pd

    rows = []
    t = _dt.datetime(start_year, 1, 1, tzinfo=_dt.timezone.utc)
    for i in range(n):
        pnl = win if (i % win_every == 0) else loss
        ts = t + _dt.timedelta(days=i * days_per_trade)
        rows.append({
            "fold": i % 3,
            "symbol": "EURUSD",
            "side": "BUY" if i % 2 else "SELL",
            "entry_time": ts.isoformat(),
            "exit_time": ts.isoformat(),
            "net_pnl": pnl,
            "pips": pnl,
        })
    return pd.DataFrame(rows)


# ============================================================ trade records

def test_trade_records_are_produced_by_a_real_fold():
    from pathlib import Path
    cache = Path("data/real/EURUSD_1d.csv")
    if not cache.exists():
        pytest.skip("no cached real data")
    candles = load_csv(cache)
    from research.backtest.walkforward_real import build_folds
    f = build_folds(candles)[1]
    o = run_fold(f.test(candles), StrategyParams(), TWO_STEP_STANDARD,
                 10000.0, 1.0, 0.3, fold_index=1)
    assert len(o.trade_records) == o.trades
    assert o.trades > 0
    for t in o.trade_records:
        assert isinstance(t, TradeRecord)
        assert t.fold == 1
        assert t.symbol == "EURUSD"
        assert t.side in ("BUY", "SELL")
        assert t.exit_time >= t.entry_time


def test_trades_frame_has_the_columns_falsification_needs():
    o = FoldOutcome(fold=0)
    df = o.trades_frame()
    for col in ("net_pnl", "exit_time", "fold"):
        assert col in df.columns


def test_empty_fold_outcome_yields_an_empty_frame():
    import pandas as pd
    df = FoldOutcome(fold=0).trades_frame()
    assert len(df) == 0
    assert isinstance(df, pd.DataFrame)


def test_net_pips_accessor_matches_records():
    ts = _dt.datetime(2024, 1, 1, tzinfo=_dt.timezone.utc)
    o = FoldOutcome(fold=0, trade_records=[
        TradeRecord(0, "EURUSD", "BUY", ts, ts, 1.1, 1.1001, 0.1, 1.0, 1.0, 1.0),
        TradeRecord(0, "EURUSD", "SELL", ts, ts, 1.1, 1.0999, 0.1, -1.0, -1.0, -1.0),
    ])
    assert o.net_pips == [1.0, -1.0]


# ========================================================== falsification

def test_no_trades_is_underpowered_never_passing():
    r = falsify_trades(None)
    assert r.verdict == "UNDERPOWERED"
    assert r.passed is False
    assert r.n_trades == 0


def test_too_few_trades_is_underpowered():
    r = falsify_trades(make_trades(n=MIN_TRADES_FOR_VERDICT - 1))
    assert r.verdict == "UNDERPOWERED"
    assert r.passed is False


def test_missing_net_pnl_column_raises():
    import pandas as pd
    with pytest.raises(ValueError):
        falsify_trades(pd.DataFrame({"exit_time": ["2024-01-01"]}))


def test_a_genuinely_robust_series_survives():
    """Profit spread evenly across years and folds should survive."""
    r = falsify_trades(make_trades(n=200, win_every=2, win=100.0, loss=-50.0))
    assert r.n_trades == 200
    assert r.checks["remove_best_year_positive"] is True
    assert r.checks["remove_top_decile_positive"] is True


def test_a_one_year_wonder_is_falsified():
    """All the profit in a single year must NOT survive."""
    import pandas as pd
    df = make_trades(n=120, win_every=10**9, loss=-10.0)   # all losers
    ts = _dt.datetime(2022, 6, 1, tzinfo=_dt.timezone.utc)
    winners = pd.DataFrame([{
        "fold": 1, "symbol": "EURUSD", "side": "BUY",
        "entry_time": ts.isoformat(), "exit_time": ts.isoformat(),
        "net_pnl": 5000.0, "pips": 5000.0,
    }])
    combined = pd.concat([df, winners], ignore_index=True)
    r = falsify_trades(combined)
    assert r.verdict == "FALSIFIED"
    assert r.checks["remove_best_year_positive"] is False


def test_outlier_driven_result_is_falsified():
    """If dropping the top decile kills it, it was outliers."""
    import pandas as pd
    df = make_trades(n=100, win_every=10**9, loss=-10.0)
    top = pd.DataFrame([{
        "fold": 0, "symbol": "EURUSD", "side": "BUY",
        "entry_time": "2021-01-01T00:00:00+00:00",
        "exit_time": "2021-01-01T00:00:00+00:00",
        "net_pnl": 900.0, "pips": 900.0,
    }] * 10)
    combined = pd.concat([df, top], ignore_index=True)
    r = falsify_trades(combined)
    assert r.checks["remove_top_decile_positive"] is False
    assert r.verdict == "FALSIFIED"


def test_report_is_json_safe():
    import json
    r = falsify_trades(make_trades(n=100))
    json.dumps(r.summary())        # must not raise


def test_render_lists_checks_and_metrics():
    r = falsify_trades(make_trades(n=100))
    text = r.render()
    for key in r.checks:
        assert key in text


def test_suite_fingerprint_is_stable_across_calls():
    a = falsify_trades(make_trades(n=100)).suite_fingerprint
    b = falsify_trades(make_trades(n=100)).suite_fingerprint
    assert a == b and len(a) == 32


def test_absent_inputs_produce_absent_checks_not_passing_ones():
    """No per-pair data must not silently become cross_pair_breadth=True."""
    r = falsify_trades(make_trades(n=100))
    assert "cross_pair_breadth" not in r.checks
    assert "cost_stress" not in r.checks
    assert "param_neighbourhood" not in r.checks


def test_optional_checks_are_used_when_supplied():
    r = falsify_trades(make_trades(n=100), per_pair={"EURUSD": 10.0},
                       cost_survival_multiple=2.0, neighbourhood_ok=True)
    assert r.checks["cross_pair_breadth"] is False   # 1 of 6 pairs is not 4
    assert r.checks["cost_stress"] is True
    assert r.checks["param_neighbourhood"] is True


def test_bootstrap_is_reported_as_a_probability():
    r = falsify_trades(make_trades(n=100))
    p = r.metrics["bootstrap_p_positive"]
    assert 0.0 <= p <= 1.0


def test_profit_factor_is_computed():
    r = falsify_trades(make_trades(n=100, win_every=2, win=100.0, loss=-50.0))
    assert r.metrics["profit_factor"] > 1.0


# ================================================== walk-forward wrapper

def test_falsify_walk_forward_on_empty_result():
    r = falsify_walk_forward(WalkForwardResult(
        params={}, initial_balance=10000.0, model_key="x",
        spread_pips=1.0, slippage_pips=0.3))
    assert r.verdict == "UNDERPOWERED"


def test_falsify_walk_forward_combines_folds():
    ts = _dt.datetime(2024, 1, 1, tzinfo=_dt.timezone.utc)
    recs = [TradeRecord(i % 2, "EURUSD", "BUY", ts, ts, 1.1, 1.1,
                        0.1, 10.0, 10.0, 10.0) for i in range(100)]
    f0 = FoldOutcome(fold=0, trades=50, trade_records=recs[:50])
    f1 = FoldOutcome(fold=1, trades=50, trade_records=recs[50:])
    res = WalkForwardResult(params={}, initial_balance=10000.0,
                            model_key="2_step_standard", spread_pips=1.0,
                            slippage_pips=0.3, folds=[f0, f1])
    r = falsify_walk_forward(res)
    assert r.n_trades == 100
    assert r.n_folds == 2


# ==================================================== real-data verdict

def test_real_data_result_is_currently_falsified():
    """A regression guard on the ACTUAL finding, not a synthetic one.

    If this ever flips, it is because the parameters or the data changed --
    which is exactly the moment a human should look.
    """
    from pathlib import Path
    cache = Path("data/real/EURUSD_1d.csv")
    if not cache.exists():
        pytest.skip("no cached real data")
    from research.backtest.walkforward_real import walk_forward
    candles = load_csv(cache)
    res = walk_forward(candles, StrategyParams(), TWO_STEP_STANDARD)
    r = falsify_walk_forward(res)
    assert r.n_trades > 100
    # The default parameter set does not survive the declared suite.
    assert r.verdict == "FALSIFIED"
    assert r.checks["remove_best_year_positive"] is False


def test_falsification_module_imports_no_execution():
    import inspect
    import research.backtest.falsify_real as m
    src = inspect.getsource(m)
    for forbidden in ("MetaTrader5", "fundingpips_mt5", "place_order"):
        assert forbidden not in src, forbidden
