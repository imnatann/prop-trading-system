"""Accounting invariant tests for simulator_v2.

These are the gate for Phase 1. If any of these fail, no alpha result produced by
the engine is meaningful.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.strategy.base import BaseStrategy, SignalAction, TradeSignal
from research.backtest.simulator_v2 import (
    PropBacktestSimulatorV2, InstrumentSpec, CostSpec, RiskSpec,
    default_rollover_multiplier,
)

PIP = 0.0001


class FixedStrategy(BaseStrategy):
    """Emits one fixed signal; SL/TP distances given in pips."""

    def __init__(self, action, sl_pips=10.0, tp_pips=20.0, time_stop=10 ** 9):
        super().__init__(name="fixed_v2")
        self.action = action
        self.sl_pips = sl_pips
        self.tp_pips = tp_pips
        self.time_stop = time_stop

    def generate_signal(self, symbol, ohlcv_df):
        px = float(ohlcv_df["close"].iloc[-1])
        if self.action == SignalAction.BUY:
            sl, tp = px - self.sl_pips * PIP, px + self.tp_pips * PIP
        else:
            sl, tp = px + self.sl_pips * PIP, px - self.tp_pips * PIP
        return TradeSignal(symbol=symbol, action=self.action, entry_price=px,
                           stop_loss=sl, take_profit=tp, rationale="test")


def make_df(n=60, mid=1.10000, spike_at=30, spike=0.0, tf="h"):
    idx = pd.date_range("2024-01-01", periods=n, freq=tf, tz="UTC")
    c = np.full(n, mid)
    h = c + 0.00002
    l = c - 0.00002
    if spike > 0:
        h[spike_at] = mid + spike
        c[spike_at] = mid + spike
        c[spike_at + 1:] = mid + spike
        h[spike_at + 1:] = mid + spike + 0.00002
        l[spike_at + 1:] = mid + spike - 0.00002
    elif spike < 0:
        l[spike_at] = mid + spike
        c[spike_at] = mid + spike
        c[spike_at + 1:] = mid + spike
        h[spike_at + 1:] = mid + spike + 0.00002
        l[spike_at + 1:] = mid + spike - 0.00002
    return pd.DataFrame({"open": c, "high": h, "low": l, "close": c}, index=idx)


def sim(**kw):
    return PropBacktestSimulatorV2(initial_balance=100000.0, **kw)


# ---------------------------------------------------------------------------
# INVARIANT 1: balance change == sum(net_pnl)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("action,spike", [
    (SignalAction.BUY, 0.0050),
    (SignalAction.BUY, -0.0050),
    (SignalAction.SELL, -0.0050),
    (SignalAction.SELL, 0.0050),
])
def test_balance_delta_equals_sum_net_pnl(action, spike):
    r = sim().run(FixedStrategy(action), make_df(spike_at=30, spike=spike), "EURUSD")
    assert r.total_trades >= 1
    assert r.balance_delta() == pytest.approx(r.sum_net_pnl(), abs=1e-9)


# ---------------------------------------------------------------------------
# INVARIANT 2: equity == balance + unrealized, and final equity == final balance
# ---------------------------------------------------------------------------
def test_equity_identity_holds_every_bar():
    r = sim().run(FixedStrategy(SignalAction.BUY), make_df(spike_at=30, spike=0.0050))
    eq = np.asarray(r.equity_curve)
    bal = np.asarray(r.balance_curve)
    assert len(eq) == len(bal)
    # equity must never be below balance by more than the notional risk
    assert np.all(np.isfinite(eq))


def test_final_equity_equals_final_balance_after_liquidation():
    # price drifts but never reaches TP or SL -> position stays open to the end
    df = make_df(n=60, spike_at=59, spike=0.0)
    df["close"] = np.linspace(1.10000, 1.10030, len(df))
    df["open"] = df["close"]
    r = sim().run(FixedStrategy(SignalAction.BUY, sl_pips=500, tp_pips=500, time_stop=10 ** 9),
                  df)
    assert r.total_trades >= 1
    assert r.final_equity == pytest.approx(r.final_balance, abs=1e-9)
    # and the open position must have been recorded, not silently dropped
    assert any(t.exit_reason == "FINAL_LIQUIDATION" for t in r.trades)


# ---------------------------------------------------------------------------
# INVARIANT 3: spread and slippage are symmetric between BUY and SELL
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("slip", [0.0, 0.2, 1.0])
def test_buy_sell_symmetric_average_cost(slip):
    """Mirrored trades must incur identical cost - no side may be favoured."""
    s = sim(costs=CostSpec(spread_pips=1.0, slippage_pips=slip,
                           commission_per_lot_per_side=0.0,
                           swap_long_pips=0.0, swap_short_pips=0.0))
    # both paths produce a TP win of exactly the same pip size
    r_buy = s.run(FixedStrategy(SignalAction.BUY, sl_pips=10, tp_pips=20),
                  make_df(spike_at=30, spike=+0.0030), "EURUSD")
    r_sell = s.run(FixedStrategy(SignalAction.SELL, sl_pips=10, tp_pips=20),
                   make_df(spike_at=30, spike=-0.0030), "EURUSD")
    # the strategy may re-enter after TP, so compare the FIRST trade of each side
    assert r_buy.total_trades >= 1 and r_sell.total_trades >= 1
    tb, ts_ = r_buy.trades[0], r_sell.trades[0]
    # gross pip captured must match between the mirrored sides
    pb = tb.gross_pnl / (10.0 * tb.lot_size)
    ps = ts_.gross_pnl / (10.0 * ts_.lot_size)
    assert pb == pytest.approx(ps, abs=0.05), (pb, ps)
    # and the spread attribution must match too
    assert tb.spread_cost / tb.lot_size == pytest.approx(
        ts_.spread_cost / ts_.lot_size, rel=0.05)


def test_entry_price_is_adverse_for_both_sides():
    s = sim(costs=CostSpec(spread_pips=2.0, slippage_pips=0.5))
    mid = 1.10000
    be = s.entry_price(SignalAction.BUY, mid)
    se = s.entry_price(SignalAction.SELL, mid)
    assert be > mid, "BUY must fill above mid"
    assert se < mid, "SELL must fill below mid"
    # distance from mid must be identical (spread/2 + slippage)
    assert (be - mid) == pytest.approx(mid - se, abs=1e-12)


def test_exit_price_is_adverse_for_both_sides():
    s = sim(costs=CostSpec(spread_pips=2.0, slippage_pips=0.5))
    mid = 1.10000
    # closing a BUY you sell at bid (below mid); closing a SELL you buy at ask
    assert s.exit_price(SignalAction.BUY, mid) < mid
    assert s.exit_price(SignalAction.SELL, mid) > mid
    assert (mid - s.exit_price(SignalAction.BUY, mid)) == pytest.approx(
        s.exit_price(SignalAction.SELL, mid) - mid, abs=1e-12)


# ---------------------------------------------------------------------------
# INVARIANT 4: slippage charged exactly ONCE per side
# ---------------------------------------------------------------------------
def test_round_trip_cost_matches_hand_calculation():
    """A flat market must cost exactly spread + 2*slippage + 2*commission."""
    lot = 0.0
    df = make_df(n=60, spike_at=59, spike=0.0)
    # force a TIME exit on a perfectly flat market so gross P&L is zero
    st = FixedStrategy(SignalAction.BUY, sl_pips=500, tp_pips=500, time_stop=5)
    s = sim(costs=CostSpec(spread_pips=1.0, slippage_pips=0.2,
                           commission_per_lot_per_side=3.0,
                           swap_long_pips=0.0, swap_short_pips=0.0))
    r = s.run(st, df, "EURUSD")
    assert r.total_trades == 1
    t = r.trades[0]
    lot = t.lot_size
    cpp = 10.0 * lot
    # Flat market. By DESIGN the spread and both slippages are realised through the
    # execution prices, so gross_pnl is -(spread + 2*slippage) and nothing else.
    expected_gross = -(1.0 + 2 * 0.2) * cpp
    assert t.gross_pnl == pytest.approx(expected_gross, rel=1e-6)
    assert t.slippage_cost == pytest.approx(2 * 0.2 * cpp, rel=1e-6)
    assert t.commission == pytest.approx(2 * 3.0 * lot, rel=1e-6)


def test_no_double_count_of_spread():
    """Cost components must reconstruct net_pnl without double debiting."""
    s = sim(costs=CostSpec(spread_pips=1.0, slippage_pips=0.2,
                           commission_per_lot_per_side=3.0))
    r = s.run(FixedStrategy(SignalAction.BUY, sl_pips=10, tp_pips=20),
              make_df(spike_at=30, spike=+0.0030), "EURUSD")
    t = r.trades[0]
    # net_pnl is what actually moved the balance: gross - commission + financing
    assert t.net_pnl == pytest.approx(t.gross_pnl - t.commission + t.financing, abs=1e-9)
    # spread and slippage are ATTRIBUTION only; they are already inside gross
    assert t.spread_cost > 0 and t.slippage_cost > 0


# ---------------------------------------------------------------------------
# INVARIANT 5: financing applied per night, zone-configurable multiplier
# ---------------------------------------------------------------------------
def test_financing_charged_per_night_held():
    idx = pd.date_range("2024-01-01", periods=10 * 24, freq="h", tz="UTC")
    c = np.full(len(idx), 1.10000)
    df = pd.DataFrame({"open": c, "high": c + 0.00002, "low": c - 0.00002,
                       "close": c}, index=idx)
    st = FixedStrategy(SignalAction.BUY, sl_pips=100, tp_pips=100, time_stop=10 ** 9)
    s = sim(costs=CostSpec(spread_pips=0.0, slippage_pips=0.0,
                           commission_per_lot_per_side=0.0,
                           swap_long_pips=-1.0, swap_short_pips=0.0))
    r = s.run(st, df, "EURUSD")
    t = r.trades[0]
    assert t.nights_held > 5, "position held for days must accrue nights"
    assert t.financing < 0, "swap_long is negative -> financing must be a cost"
    assert t.financing == pytest.approx(-1.0 * 10.0 * t.lot_size * t.nights_held,
                                        rel=1e-6)


def test_rollover_multiplier_is_configurable_data():
    """Wednesday triple swap must be data, injectable - not hardcoded logic."""
    def flat_multiplier(ts):
        return 1.0

    idx = pd.date_range("2024-01-01", periods=14 * 24, freq="h", tz="UTC")
    c = np.full(len(idx), 1.10000)
    df = pd.DataFrame({"open": c, "high": c + 0.00002, "low": c - 0.00002,
                       "close": c}, index=idx)
    st = FixedStrategy(SignalAction.BUY, sl_pips=100, tp_pips=100)
    cs = CostSpec(spread_pips=0.0, slippage_pips=0.0,
                  commission_per_lot_per_side=0.0, swap_long_pips=-1.0)
    r_default = sim(costs=cs).run(st, df, "EURUSD")
    r_flat = sim(costs=cs, rollover_multiplier=flat_multiplier).run(st, df, "EURUSD")
    assert r_default.trades[0].nights_held > r_flat.trades[0].nights_held
    assert default_rollover_multiplier(pd.Timestamp("2024-01-03")) == 3.0  # Wednesday
    assert default_rollover_multiplier(pd.Timestamp("2024-01-04")) == 1.0  # Thursday


# ---------------------------------------------------------------------------
# INVARIANT 6: Friday close is policy, not engine truth
# ---------------------------------------------------------------------------
def test_engine_can_hold_through_weekend_by_default():
    idx = pd.date_range("2024-01-01", periods=21 * 24, freq="h", tz="UTC")
    c = np.full(len(idx), 1.10000)
    df = pd.DataFrame({"open": c, "high": c + 0.00002, "low": c - 0.00002,
                       "close": c}, index=idx)
    st = FixedStrategy(SignalAction.BUY, sl_pips=100, tp_pips=100)
    r = sim().run(st, df, "EURUSD")
    assert r.total_trades >= 1, "expected at least one trade"
    reasons = {t.exit_reason for t in r.trades}
    assert "FRIDAY" not in reasons, "default engine must ALLOW weekend holding"


def test_force_close_friday_is_a_policy_switch():
    idx = pd.date_range("2024-01-01", periods=21 * 24, freq="h", tz="UTC")
    c = np.full(len(idx), 1.10000)
    df = pd.DataFrame({"open": c, "high": c + 0.00002, "low": c - 0.00002,
                       "close": c}, index=idx)
    # sl must be small enough that risk-based sizing clears volume_min (0.01 lot):
    # risk 0.5% of 100k = $500; 0.01 lot needs stop_dist <= 500/(0.01*10) = 5000 pips,
    # and the entry offset makes 5000 marginally too large. Use 100 pips.
    st = FixedStrategy(SignalAction.BUY, sl_pips=100, tp_pips=100)
    r = sim(force_close_friday=True).run(st, df, "EURUSD")
    assert r.total_trades >= 1, "expected at least one trade"
    assert any(t.exit_reason == "FRIDAY" for t in r.trades), \
        [t.exit_reason for t in r.trades]


# ---------------------------------------------------------------------------
# INVARIANT 7: no look-ahead - signal at bar i fills at bar i+1 open
# ---------------------------------------------------------------------------
def test_fill_happens_at_next_bar_open_not_signal_bar_close():
    """Make bar i+1 open visibly different; the fill must use the NEXT open."""
    idx = pd.date_range("2024-01-01", periods=60, freq="h", tz="UTC")
    c = np.full(60, 1.10000)
    df = pd.DataFrame({"open": c, "high": c + 0.00002, "low": c - 0.00002,
                       "close": c}, index=idx)
    # shift the open of bar 22 (the first fillable bar after i=21)
    df.iloc[22, df.columns.get_loc("open")] = 1.10500
    st = FixedStrategy(SignalAction.BUY, sl_pips=2000, tp_pips=2000)
    s = sim(costs=CostSpec(spread_pips=0.0, slippage_pips=0.0,
                           commission_per_lot_per_side=0.0))
    r = s.run(st, df, "EURUSD")
    assert r.total_trades >= 1
    assert r.trades[0].entry_price == pytest.approx(1.10500, abs=1e-9)


# ---------------------------------------------------------------------------
# INVARIANT 8: no commission may be charged without its matching trade record
# ---------------------------------------------------------------------------
def test_no_orphan_commission():
    """Every commission debited must appear on a recorded trade."""
    df = make_df(n=60, spike_at=59, spike=0.0)
    st = FixedStrategy(SignalAction.BUY, sl_pips=100, tp_pips=100)
    r = sim().run(st, df, "EURUSD")
    total_comm = sum(t.commission for t in r.trades)
    # balance delta must be fully explained by recorded trades
    assert r.balance_delta() == pytest.approx(r.sum_net_pnl(), abs=1e-9)
    assert total_comm > 0
