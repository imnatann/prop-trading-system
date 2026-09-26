"""Phase 2 unit tests for Alpha v2 MTF.

ALL fixtures are synthetic. No real market data is loaded, and nothing here computes
performance. The purpose is to prove the plumbing and the no-look-ahead discipline,
NOT that the strategy works.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.strategy.alpha_v2_mtf import (
    AlphaV2Config, AlphaV2MTFStrategy, FROZEN_GRID, frozen_grid,
    h4_regime, h1_setup_long, h1_setup_short, m15_breakout_long,
    m15_breakout_short, risk_levels, attach_m15_atr,
)
from src.strategy.base import SignalAction
from src.risk.exante_gates import (
    CostEstimate, ExAnteEstimate, estimate_cost, cost_gate, edge_from_training,
    fit_vr_thresholds, vr_veto, SessionFilter,
)
from research.mtf import build_mtf_frame

PIP = 0.0001


# ---------------------------------------------------------------------------
# Synthetic MTF frame builders
# ---------------------------------------------------------------------------
def synth_m15(n=4000, start="2024-01-01", drift=0.0, seed=3,
              trend_amp=0.00030, noise=0.00045, cycle=900) -> pd.DataFrame:
    """Synthetic M15 bars with BOTH a trend and a slow cycle.

    A pure drift produces a market that never pulls back, so the H1 pullback gate
    never opens and the M15 breakout can never be tested. Real trending markets
    oscillate around the trend, so the fixture must too. The cycle period is long
    relative to the breakout window so the two do not cancel out.
    """
    idx = pd.date_range(start, periods=n, freq="15min", tz="UTC")
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    cycle = trend_amp * np.sin(2 * np.pi * t / cycle)
    ret = rng.normal(drift, noise, n) + cycle
    close = 1.10 * np.exp(np.cumsum(ret))
    open_ = np.concatenate([[close[0]], close[:-1]])
    hi = np.maximum(close, open_) * (1 + np.abs(rng.normal(0, 0.00012, n)))
    lo = np.minimum(close, open_) * (1 - np.abs(rng.normal(0, 0.00012, n)))
    return pd.DataFrame({"open": open_, "high": hi, "low": lo, "close": close},
                        index=idx)


def mtf_frame(**kw) -> pd.DataFrame:
    df = attach_m15_atr(build_mtf_frame(synth_m15(**kw), "1h", "4h"))
    return df


# ---------------------------------------------------------------------------
# 1. Frozen grid discipline
# ---------------------------------------------------------------------------
def test_frozen_grid_has_exactly_four_configs():
    g = frozen_grid()
    assert len(g) == 4, "protocol fixes the search space at FOUR configurations"
    # unique names, unique (pullback, breakout) pairs
    assert len({c.name for c in g}) == 4
    assert len({(c.pullback_atr, c.breakout_bars) for c in g}) == 4


def test_frozen_grid_values_match_protocol():
    combos = sorted((c.pullback_atr, c.breakout_bars) for c in FROZEN_GRID)
    assert combos == [(0.5, 8), (0.5, 20), (1.0, 8), (1.0, 20)]
    for c in FROZEN_GRID:
        assert c.atr_stop_mult == 1.5
        assert c.r_multiple == 2.0
        assert c.time_stop_bars == 96


def test_config_is_immutable():
    """A frozen grid must not be editable at runtime."""
    with pytest.raises(Exception):
        FROZEN_GRID[0].pullback_atr = 9.9  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 2. Regime / setup / trigger logic on hand-built inputs
# ---------------------------------------------------------------------------
def test_h4_regime_direction():
    up = h4_regime(fast=1.20, slow=1.10, close=1.25)
    assert up.long_ok and not up.short_ok
    dn = h4_regime(fast=1.10, slow=1.20, close=1.05)
    assert dn.short_ok and not dn.long_ok
    nan = h4_regime(np.nan, 1.1, 1.2)
    assert not nan.long_ok and not nan.short_ok


def test_h1_pullback_detection_is_symmetric():
    # long setup: price below fast EMA by >= pullback*ATR
    assert h1_setup_long(h1_close=1.0950, h1_fast=1.1000, h1_atr=0.0050,
                         pullback_atr=1.0)
    assert not h1_setup_long(h1_close=1.0999, h1_fast=1.1000, h1_atr=0.0050,
                             pullback_atr=1.0)
    # short setup mirrors it
    assert h1_setup_short(h1_close=1.1050, h1_fast=1.1000, h1_atr=0.0050,
                          pullback_atr=1.0)
    assert not h1_setup_short(h1_close=1.1001, h1_fast=1.1000, h1_atr=0.0050,
                              pullback_atr=1.0)


def test_h1_setup_rejects_degenerate_atr():
    assert not h1_setup_long(1.0, 1.1, 0.0, 0.5)
    assert not h1_setup_short(1.0, 1.1, np.nan, 0.5)


def test_m15_breakout_excludes_the_current_bar():
    highs = np.array([1.0, 1.1, 1.2, 1.3, 1.4, 5.0])   # last bar is the "current" one
    # looking back 3 bars must use [1.3, 1.4] window excluding index -1
    assert m15_breakout_long(highs, close=1.45, n=3) is True
    assert m15_breakout_long(highs, close=1.35, n=3) is False
    # if the current bar's own high were included, 1.45 would not break 5.0
    assert m15_breakout_long(highs, close=4.0, n=3) is True


def test_m15_breakdown_is_symmetric():
    lows = np.array([1.0, 0.9, 0.8, 0.7, 0.6, 0.1])
    assert m15_breakout_short(lows, close=0.55, n=3) is True
    assert m15_breakout_short(lows, close=0.65, n=3) is False


def test_breakout_needs_enough_history():
    assert m15_breakout_long(np.array([1.0, 1.1]), 2.0, n=10) is False


# ---------------------------------------------------------------------------
# 3. Risk levels
# ---------------------------------------------------------------------------
def test_risk_levels_respects_r_multiple():
    cfg = AlphaV2Config("t", pullback_atr=0.5, breakout_bars=8)
    sl, tp = risk_levels(SignalAction.BUY, 1.1000, 0.0010, cfg)
    assert sl == pytest.approx(1.1000 - 1.5 * 0.0010)
    assert tp == pytest.approx(1.1000 + 1.5 * 0.0010 * 2.0)
    # R multiple is exactly 2.0 by construction
    r = (tp - 1.1000) / (1.1000 - sl)
    assert r == pytest.approx(2.0)


def test_risk_levels_sell_is_mirror():
    cfg = AlphaV2Config("t", pullback_atr=0.5, breakout_bars=8)
    sl, tp = risk_levels(SignalAction.SELL, 1.1000, 0.0010, cfg)
    assert sl > 1.1000 > tp
    assert (sl - 1.1000) / (1.1000 - tp) == pytest.approx(0.5)


def test_risk_levels_rejects_bad_atr():
    cfg = AlphaV2Config("t", pullback_atr=0.5, breakout_bars=8)
    assert risk_levels(SignalAction.BUY, 1.1, 0.0, cfg) is None
    assert risk_levels(SignalAction.BUY, 1.1, np.nan, cfg) is None


# ---------------------------------------------------------------------------
# 4. Strategy contract
# ---------------------------------------------------------------------------
def test_strategy_requires_mtf_columns():
    cfg = frozen_grid()[0]
    st = AlphaV2MTFStrategy(cfg)
    plain = synth_m15(n=100)
    with pytest.raises(ValueError, match="MTF columns"):
        st.generate_signal("EURUSD", plain)


def test_strategy_returns_hold_when_regime_flat():
    cfg = frozen_grid()[0]
    st = AlphaV2MTFStrategy(cfg)
    df = mtf_frame(n=600)
    # force a flat H4 regime
    df["h4_ema_fast"] = 1.0
    df["h4_ema_slow"] = 1.0
    sig = st.generate_signal("EURUSD", df)
    assert sig.action == SignalAction.HOLD


def test_strategy_signal_always_has_valid_levels():
    """Wherever a BUY/SELL is emitted, SL/TP must bracket the entry at R=2."""
    for cfg in frozen_grid():
        st = AlphaV2MTFStrategy(cfg)
        df = mtf_frame(n=6000, drift=0.00002, seed=11, cycle=700)
        fired = 0
        for i in range(300, len(df), 3):
            sig = st.generate_signal("EURUSD", df.iloc[: i + 1])
            if sig.action == SignalAction.HOLD:
                continue
            fired += 1
            e = sig.entry_price
            if sig.action == SignalAction.BUY:
                assert sig.stop_loss < e < sig.take_profit
                assert (sig.take_profit - e) / (e - sig.stop_loss) == pytest.approx(2.0,
                                                                                 rel=0.02)
            else:
                assert sig.take_profit < e < sig.stop_loss
        assert fired > 0, "config %s never fired on an oscillating-trend fixture" % cfg.name


def test_strategy_sets_time_stop():
    cfg = frozen_grid()[0]
    st = AlphaV2MTFStrategy(cfg)
    df = mtf_frame(n=3000, drift=0.00002, seed=11)
    got = None
    for i in range(300, len(df), 5):
        sig = st.generate_signal("EURUSD", df.iloc[: i + 1])
        if sig.action != SignalAction.HOLD:
            got = sig; break
    assert got is not None
    assert got.time_stop == cfg.time_stop_bars


def test_signal_is_deterministic():
    """Identical input must give identical output - no hidden RNG or state."""
    cfg = frozen_grid()[2]
    df = mtf_frame(n=2500, drift=0.00002, seed=5)
    a = [AlphaV2MTFStrategy(cfg).generate_signal("EURUSD", df.iloc[: i + 1]).action
         for i in range(400, 1200, 13)]
    b = [AlphaV2MTFStrategy(cfg).generate_signal("EURUSD", df.iloc[: i + 1]).action
         for i in range(400, 1200, 13)]
    assert a == b


# ---------------------------------------------------------------------------
# 5. Look-ahead: truncating the future must not change past signals
# ---------------------------------------------------------------------------
def test_signal_is_invariant_to_future_data():
    """The strongest practical look-ahead test.

    Compute signals on the full frame, then on a frame truncated at T. Every signal
    at or before T must be IDENTICAL. If any future bar influenced a past decision,
    these would differ.
    """
    cfg = frozen_grid()[0]
    full = mtf_frame(n=2600, drift=0.00002, seed=9)
    cut_at = 1500
    truncated = full.iloc[:cut_at]
    st = AlphaV2MTFStrategy(cfg)

    mismatches = 0
    checked = 0
    for i in range(400, cut_at - 5, 23):
        a = st.generate_signal("EURUSD", full.iloc[: i + 1])
        b = st.generate_signal("EURUSD", truncated.iloc[: i + 1])
        checked += 1
        if (a.action, a.stop_loss, a.take_profit) != (b.action, b.stop_loss, b.take_profit):
            mismatches += 1
    assert checked > 30
    assert mismatches == 0, "%d/%d signals changed when future bars were removed" % (
        mismatches, checked)


def test_htf_columns_do_not_change_before_their_close():
    """Truncating strictly inside an H4 bar must not alter earlier H4 context."""
    full = mtf_frame(n=1600, seed=21)
    cut = 1200
    part = full.iloc[:cut]
    seg_full = full["h4_ema_fast"].iloc[800:cut]
    seg_part = part["h4_ema_fast"].iloc[800:cut]
    both = seg_full.notna() & seg_part.notna()
    assert both.sum() > 100
    np.testing.assert_allclose(seg_full[both].values, seg_part[both].values,
                               rtol=1e-12, atol=1e-15)


# ---------------------------------------------------------------------------
# 6. Ex-ante cost gate
# ---------------------------------------------------------------------------
def test_cost_estimate_components():
    c = estimate_cost(spread_pips=1.0, slippage_pips=0.2,
                      commission_per_lot_per_side=3.0, lot_size=0.10,
                      pip_value_per_lot=10.0, expected_nights=1.0,
                      swap_pips_per_night=-0.5)
    assert c.spread_pips == pytest.approx(1.0)
    assert c.slippage_pips == pytest.approx(0.4)     # round trip
    assert c.commission_pips == pytest.approx(0.6)   # 2*3*0.10 / 1.0
    assert c.financing_pips == pytest.approx(0.5)
    assert c.total_pips == pytest.approx(2.5)


def test_cost_gate_requires_multiple_of_cost_not_mere_break_even():
    c = estimate_cost(spread_pips=1.0, slippage_pips=0.2,
                      commission_per_lot_per_side=3.0, lot_size=0.10,
                      expected_nights=0.0)
    # an edge barely above cost must still be REJECTED at 2x safety
    just_over = ExAnteEstimate(edge_pips=c.total_pips * 1.01,
                               trained_on=("2020-01-01", "2021-12-31"),
                               n_observations=100)
    assert cost_gate(just_over, c, safety_multiple=2.0).allowed is False
    comfortably = ExAnteEstimate(edge_pips=c.total_pips * 2.5,
                                 trained_on=("2020-01-01", "2021-12-31"),
                                 n_observations=100)
    assert cost_gate(comfortably, c, safety_multiple=2.0).allowed is True


def test_exante_estimate_requires_provenance():
    """An estimate without a declared training window must be impossible to build."""
    with pytest.raises(ValueError):
        ExAnteEstimate(edge_pips=5.0, trained_on=None, n_observations=10)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ExAnteEstimate(edge_pips=5.0, trained_on=("a", "b"), n_observations=0)


def test_edge_from_training_uses_lower_confidence_bound():
    noisy = edge_from_training([3.0, -3.0, 3.0, -3.0, 3.0], ("a", "b"))
    clean = edge_from_training([3.0, 3.1, 2.9, 3.0, 3.05], ("a", "b"))
    assert noisy.edge_pips < clean.edge_pips, "noise must be penalised"
    assert noisy.edge_pips < 0.6, "a high-variance sample must not look like an edge"
    assert noisy.n_observations == 5 and noisy.trained_on == ("a", "b")


def test_edge_from_training_rejects_empty():
    with pytest.raises(ValueError):
        edge_from_training([], ("a", "b"))
    with pytest.raises(ValueError):
        edge_from_training([np.nan], ("a", "b"))


# ---------------------------------------------------------------------------
# 7. VR veto
# ---------------------------------------------------------------------------
def test_vr_thresholds_are_percentile_of_training_only():
    train = list(np.linspace(0.01, 0.20, 100))
    th = fit_vr_thresholds(train, train, ("2015-01-01", "2019-12-31"), percentile=20.0)
    assert th.sd_vr_p20 == pytest.approx(np.percentile(train, 20.0))
    assert th.trained_on == ("2015-01-01", "2019-12-31")


def test_vr_veto_blocks_random_walk_regime():
    train = list(np.linspace(0.01, 0.20, 100))
    th = fit_vr_thresholds(train, train, ("a", "b"))
    # basket mean right at 1.0 -> veto
    assert vr_veto(sd_vr_now=0.15, mean_vr_now=1.001, th=th).allowed is False
    # dispersion collapsed below train P20 -> veto
    assert vr_veto(sd_vr_now=0.005, mean_vr_now=0.90, th=th).allowed is False
    # trending / dispersive regime -> allowed
    assert vr_veto(sd_vr_now=0.15, mean_vr_now=0.90, th=th).allowed is True


def test_vr_veto_blocks_on_undefined_statistics():
    th = fit_vr_thresholds([0.01, 0.2], [0.9, 1.1], ("a", "b"))
    assert vr_veto(np.nan, 0.9, th).allowed is False
    assert vr_veto(0.1, np.nan, th).allowed is False


def test_fit_vr_thresholds_rejects_empty():
    with pytest.raises(ValueError):
        fit_vr_thresholds([], [], ("a", "b"))


# ---------------------------------------------------------------------------
# 8. Session filter + timezone / DST
# ---------------------------------------------------------------------------
def test_session_filter_allows_liquid_hours_only():
    sf = SessionFilter(start_hour_utc=7, end_hour_utc=20)
    assert sf.allows(pd.Timestamp("2024-01-03 07:00", tz="UTC"))
    assert sf.allows(pd.Timestamp("2024-01-03 19:59", tz="UTC"))
    assert not sf.allows(pd.Timestamp("2024-01-03 06:59", tz="UTC"))
    assert not sf.allows(pd.Timestamp("2024-01-03 20:00", tz="UTC"))


def test_session_filter_blocks_weekend():
    sf = SessionFilter()
    assert not sf.allows(pd.Timestamp("2024-01-06 12:00", tz="UTC"))  # Saturday
    assert not sf.allows(pd.Timestamp("2024-01-07 12:00", tz="UTC"))  # Sunday


def test_session_filter_is_timezone_correct():
    """A non-UTC timestamp must be converted, not misread as UTC."""
    sf = SessionFilter(start_hour_utc=7, end_hour_utc=20)
    # 10:00 in New York (EST, UTC-5) is 15:00 UTC -> inside
    ny = pd.Timestamp("2024-01-03 10:00", tz="America/New_York")
    assert sf.allows(ny)
    # 02:00 New York is 07:00 UTC -> inside; 01:00 is 06:00 UTC -> outside
    assert sf.allows(pd.Timestamp("2024-01-03 02:00", tz="America/New_York"))
    assert not sf.allows(pd.Timestamp("2024-01-03 01:00", tz="America/New_York"))


def test_session_filter_handles_dst_transition():
    """US DST starts 2024-03-10. The same NY wall-clock time maps to different UTC."""
    sf = SessionFilter(start_hour_utc=7, end_hour_utc=20)
    before = pd.Timestamp("2024-03-08 09:00", tz="America/New_York")  # EST -> 14:00Z
    after = pd.Timestamp("2024-03-13 09:00", tz="America/New_York")   # EDT -> 13:00Z
    assert before.utcoffset() != after.utcoffset(), "fixture must straddle DST"
    assert sf.allows(before) and sf.allows(after)
    # 02:00 NY is 07:00Z in EST but 06:00Z in EDT -> allowed before, blocked after
    est2 = pd.Timestamp("2024-03-08 02:00", tz="America/New_York")
    edt2 = pd.Timestamp("2024-03-13 02:00", tz="America/New_York")
    assert sf.allows(est2) is True
    assert sf.allows(edt2) is False


def test_naive_timestamp_is_treated_as_utc_not_silently_dropped():
    sf = SessionFilter(start_hour_utc=7, end_hour_utc=20)
    assert sf.allows(pd.Timestamp("2024-01-03 10:00")) is True
    assert sf.allows(pd.Timestamp("2024-01-03 03:00")) is False
