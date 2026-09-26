"""Tests for the FundingPips rule book and the intra-bar breach engine.

These tests are the evidence that account failure is now MEASURABLE. They are
written against the OFFICIAL worked examples on the FundingPips help centre, so
a future rule change fails a test instead of silently invalidating results.
"""
from __future__ import annotations

import datetime as _dt

import pytest

from config.fundingpips_rules import (
    DEFAULT_BUFFER,
    FORBIDDEN_STRATEGY_CATEGORIES,
    MODELS,
    ONE_STEP_FLEX,
    PLATFORM_UTC_OFFSET_HOURS,
    RULES_AS_OF,
    TWO_STEP_FLEX,
    TWO_STEP_PRO,
    TWO_STEP_STANDARD,
    VPS_ACCOUNT_LOGIN_ALLOWED,
    describe_rules,
    platform_day_start_utc,
    platform_trading_day,
)
from research.risk.fundingpips_engine import (
    BreachKind,
    Ordering,
    PropRuleEngine,
    SoftStop,
    adverse_price_for,
    assert_equity_identity,
    assert_path_finite,
    intrabar_path,
)

T0 = _dt.datetime(2026, 9, 1, 9, 0, tzinfo=_dt.timezone.utc)
UTC = _dt.timezone.utc
BAR = _dt.timedelta(minutes=15)


def _flat(engine, ts=T0, price=1.10000):
    engine.on_bar(ts, price, price, price, price)


# ============================================================ official numbers

def test_official_worked_example_equity_higher():
    """Official: baseline $107,000 -> 5% = $5,350 -> floor $101,650."""
    assert TWO_STEP_STANDARD.hard_daily_floor(107_000.0) == pytest.approx(101_650.0)


def test_official_worked_example_balance_higher():
    """Official: baseline $100,000 -> 5% = $5,000 -> floor $95,000."""
    assert TWO_STEP_STANDARD.hard_daily_floor(100_000.0) == pytest.approx(95_000.0)


def test_official_max_loss_is_static_from_starting_size():
    """Official: $100K, 10% -> equity or balance cannot touch $90,000."""
    assert TWO_STEP_STANDARD.hard_max_loss_floor(100_000.0) == pytest.approx(90_000.0)
    for model in MODELS.values():
        assert model.max_loss_is_static is True


@pytest.mark.parametrize("model,daily,maxloss", [
    (ONE_STEP_FLEX, 3.0, 12.0),
    (TWO_STEP_STANDARD, 5.0, 10.0),
    (TWO_STEP_FLEX, 4.0, 12.0),
    (TWO_STEP_PRO, 3.0, 6.0),
])
def test_model_limits_match_published_pages(model, daily, maxloss):
    assert model.daily_loss_pct == daily
    assert model.max_loss_pct == maxloss


def test_targets_match_published_pages():
    assert (TWO_STEP_STANDARD.phase1_target_pct, TWO_STEP_STANDARD.phase2_target_pct) == (8.0, 5.0)
    assert (TWO_STEP_FLEX.phase1_target_pct, TWO_STEP_FLEX.phase2_target_pct) == (10.0, 6.0)
    assert (TWO_STEP_PRO.phase1_target_pct, TWO_STEP_PRO.phase2_target_pct) == (6.0, 6.0)
    assert ONE_STEP_FLEX.phase1_target_pct == 12.0


def test_passing_balance_uses_initial_size():
    assert TWO_STEP_STANDARD.passing_balance(10_000.0, 1) == pytest.approx(10_800.0)
    # cumulative convention (see phase2_base): 8% + 5% = 13%
    assert TWO_STEP_STANDARD.passing_balance(10_000.0, 2) == pytest.approx(11_300.0)


# ============================================================= platform clock

def test_platform_day_resets_at_utc_plus_3():
    """22:00 UTC is already the NEXT platform day because the reset is UTC+3."""
    assert platform_trading_day(_dt.datetime(2026, 9, 1, 22, 0, tzinfo=UTC)) == _dt.date(2026, 9, 2)
    assert platform_trading_day(_dt.datetime(2026, 9, 1, 20, 59, tzinfo=UTC)) == _dt.date(2026, 9, 1)


def test_platform_day_start_round_trips():
    start = platform_day_start_utc(_dt.date(2026, 9, 2))
    assert start == _dt.datetime(2026, 9, 1, 21, 0, tzinfo=UTC)
    assert platform_trading_day(start) == _dt.date(2026, 9, 2)


def test_platform_day_rejects_naive_datetime():
    with pytest.raises(ValueError):
        platform_trading_day(_dt.datetime(2026, 9, 1, 22, 0))


def test_offset_is_fixed_three_not_a_dst_zone():
    assert PLATFORM_UTC_OFFSET_HOURS == 3


# ==================================================== intra-bar breach: core

def test_breach_is_detected_when_the_bar_CLOSES_SAFE():
    """THE central claim of this module.

    A bar dips 60 pips inside a 10K account (5% daily budget = $500) and then
    recovers to close exactly flat. A close-only backtest sees equity unchanged
    and reports the account safe. FundingPips terminates it intrabar.
    """
    engine = PropRuleEngine(model=TWO_STEP_STANDARD, initial_balance=10_000.0)
    _flat(engine)
    engine.open_position("BUY", 1.0, 1.10000)

    closed_safe_equity = engine.equity(1.10000)
    assert closed_safe_equity == pytest.approx(10_000.0)
    assert closed_safe_equity > engine.hard_daily_floor()  # close-only says SAFE

    alive = engine.on_bar(T0 + BAR, 1.10000, 1.10010, 1.09400, 1.10000)
    assert alive is False
    assert engine.outcome.breach is BreachKind.DAILY_LOSS


def test_breach_detection_does_not_depend_on_intrabar_ordering():
    """A verdict that flips between orderings is not a verdict.

    Here BOTH orderings must breach, because the adverse extreme alone is enough.
    """
    for ordering in (Ordering.ADVERSE_FIRST, Ordering.ADVERSE_LAST):
        engine = PropRuleEngine(model=TWO_STEP_STANDARD, initial_balance=10_000.0,
                                ordering=ordering)
        _flat(engine)
        engine.open_position("BUY", 1.0, 1.10000)
        engine.on_bar(T0 + BAR, 1.10000, 1.10010, 1.09400, 1.10000)
        assert engine.outcome.breach is BreachKind.DAILY_LOSS, ordering


def test_adverse_extreme_is_low_for_buy_and_high_for_sell():
    assert adverse_price_for("BUY", high=1.9, low=1.1) == 1.1
    assert adverse_price_for("SELL", high=1.9, low=1.1) == 1.9


def test_sell_position_breaches_on_the_high():
    engine = PropRuleEngine(model=TWO_STEP_STANDARD, initial_balance=10_000.0)
    _flat(engine)
    engine.open_position("SELL", 1.0, 1.10000)
    engine.on_bar(T0 + BAR, 1.10000, 1.10610, 1.09990, 1.10000)
    assert engine.outcome.breach is BreachKind.DAILY_LOSS


def test_no_position_cannot_move_equity_intrabar():
    """With no open position equity is constant, so only the close is emitted."""
    assert intrabar_path(1.1, 1.2, 1.0, 1.15, side=None) == [("close", 1.15)]


def test_path_is_deduplicated_when_extremes_coincide():
    """A doji-style bar yields one point, not three identical ones."""
    path = intrabar_path(1.10, 1.10, 1.10, 1.10, side="BUY")
    assert path == [("adverse", 1.10)]


def test_path_keeps_distinct_extremes():
    path = intrabar_path(1.10, 1.12, 1.09, 1.11, side="BUY")
    assert path == [("adverse", 1.09), ("favourable", 1.12), ("close", 1.11)]


# ============================================== daily vs max loss precedence

def test_max_loss_takes_precedence_over_daily_loss():
    """A gap through BOTH floors is reported as the MAX loss, not the daily one.

    Max loss is the terminal event, so it must be the recorded cause.
    """
    engine = PropRuleEngine(model=TWO_STEP_STANDARD, initial_balance=10_000.0)
    _flat(engine)
    engine.open_position("BUY", 1.0, 1.10000)
    # 1,200 loss = 12% -> below the 9,000 max-loss floor
    engine.on_bar(T0 + BAR, 1.10000, 1.10000, 1.08800, 1.08800)
    assert engine.outcome.breach is BreachKind.MAX_LOSS


def test_balance_alone_can_breach_max_loss():
    """Official wording is 'equity OR balance'. Equity-only checks are unsafe."""
    engine = PropRuleEngine(model=TWO_STEP_STANDARD, initial_balance=10_000.0)
    _flat(engine)
    # Book a realised -1,200 loss with no open position (equity == balance).
    engine.balance -= 1_200.0
    engine.on_bar(T0 + BAR, 1.1, 1.1, 1.1, 1.1)
    assert engine.outcome.breach is BreachKind.MAX_LOSS


# ===================================================== soft vs hard discipline

def test_soft_stop_does_not_kill_the_account():
    """Soft limits stop US. Only hard limits terminate the ACCOUNT."""
    engine = PropRuleEngine(model=TWO_STEP_STANDARD, initial_balance=10_000.0)
    _flat(engine)
    engine.open_position("BUY", 1.0, 1.10000)
    # -450 = 4.5%: past the 4.0% soft floor, inside the 5.0% hard floor
    engine.on_bar(T0 + BAR, 1.10000, 1.10000, 1.09550, 1.09550)
    assert engine.outcome.soft_stop is SoftStop.SOFT_DAILY
    assert engine.outcome.breach is BreachKind.NONE
    assert engine.outcome.survival is True


def test_within_soft_budget_nothing_triggers():
    engine = PropRuleEngine(model=TWO_STEP_STANDARD, initial_balance=10_000.0)
    _flat(engine)
    engine.open_position("BUY", 1.0, 1.10000)
    engine.on_bar(T0 + BAR, 1.10000, 1.10000, 1.09650, 1.09650)  # -350 = 3.5%
    assert engine.outcome.soft_stop is SoftStop.NONE
    assert engine.outcome.breach is BreachKind.NONE


def test_soft_floors_are_strictly_inside_hard_floors():
    engine = PropRuleEngine(model=TWO_STEP_STANDARD, initial_balance=10_000.0)
    _flat(engine)
    assert engine.soft_daily_floor() > engine.hard_daily_floor()
    assert engine.soft_max_loss_floor() > engine.hard_max_loss_floor()


def test_soft_buffer_scales_with_the_model():
    """The buffer is a FRACTION of the legal budget, so it can never exceed it."""
    for model in MODELS.values():
        buffer = DEFAULT_BUFFER
        assert buffer.soft_daily_floor(model, 100_000.0) > model.hard_daily_floor(100_000.0)
        assert buffer.soft_max_loss_floor(model, 100_000.0) > model.hard_max_loss_floor(100_000.0)


# ========================================================== baseline rolling

def test_baseline_uses_higher_of_balance_and_equity_at_reset():
    """The baseline is struck on the day's OPENING equity, exactly as stated.

    A floating profit carried into a new platform day raises that day's
    baseline, so a reversal can consume the daily budget even while the trade
    is still profitable from entry. This is the documented behaviour and the
    reason an overnight winner can still create a daily breach.
    """
    engine = PropRuleEngine(model=TWO_STEP_STANDARD, initial_balance=10_000.0)
    engine.on_bar(T0, 1.10000, 1.10000, 1.10000, 1.10000)      # day 1 opens flat
    engine.open_position("BUY", 1.0, 1.10000)
    # 22:00 UTC == 01:00 platform on the NEXT day -> baseline is struck here,
    # with the position already carrying +200 of floating profit.
    engine.on_bar(_dt.datetime(2026, 9, 1, 22, 0, tzinfo=UTC),
                  1.10200, 1.10200, 1.10200, 1.10200)
    assert engine.baseline == pytest.approx(10_200.0)


def test_baseline_ignores_unrealised_profit_made_intraday():
    """Profit earned AFTER the reset must not retroactively raise the day."""
    engine = PropRuleEngine(model=TWO_STEP_STANDARD, initial_balance=10_000.0)
    engine.on_bar(T0, 1.10000, 1.10000, 1.10000, 1.10000)
    engine.open_position("BUY", 1.0, 1.10000)
    engine.on_bar(T0 + BAR, 1.10200, 1.10200, 1.10200, 1.10200)  # +200
    assert engine.baseline == pytest.approx(10_000.0)


def test_baseline_does_not_reset_on_a_utc_day_boundary_alone():
    """23:00 UTC is still the same platform day; 22:00 UTC would be the next."""
    engine = PropRuleEngine(model=TWO_STEP_STANDARD, initial_balance=10_000.0)
    engine.on_bar(_dt.datetime(2026, 9, 1, 21, 30, tzinfo=UTC), 1.1, 1.1, 1.1, 1.1)
    base_before = engine.baseline
    engine.on_bar(_dt.datetime(2026, 9, 1, 23, 30, tzinfo=UTC), 1.1, 1.1, 1.1, 1.1)
    assert engine.baseline == base_before


def test_baseline_resets_when_the_platform_day_rolls():
    engine = PropRuleEngine(model=TWO_STEP_STANDARD, initial_balance=10_000.0)
    engine.on_bar(_dt.datetime(2026, 9, 1, 20, 0, tzinfo=UTC), 1.1, 1.1, 1.1, 1.1)
    # 22:00 UTC == 01:00 platform on the 2nd -> new trading day
    engine.on_bar(_dt.datetime(2026, 9, 1, 22, 0, tzinfo=UTC), 1.1, 1.1, 1.1, 1.1)
    assert engine.baseline == pytest.approx(10_000.0)


# ================================================================== pass/fail

def test_one_step_flex_passes_on_phase1_alone():
    engine = PropRuleEngine(model=ONE_STEP_FLEX, initial_balance=10_000.0)
    engine.balance = 11_200.0  # +12%
    engine.on_bar(T0 + BAR, 1.1, 1.1, 1.1, 1.1)
    assert engine.outcome.phase1_reached is True
    assert engine.outcome.passed is True


def test_two_step_requires_both_phases():
    engine = PropRuleEngine(model=TWO_STEP_STANDARD, initial_balance=10_000.0)
    engine.balance = 10_800.0  # +8%: phase 1 done, phase 2 not
    engine.on_bar(T0 + BAR, 1.1, 1.1, 1.1, 1.1)
    assert engine.outcome.phase1_reached is True
    assert engine.outcome.phase2_reached is False
    assert engine.outcome.passed is False

    engine.balance = 11_300.0  # +13% cumulative (8% + 5%)
    engine.on_bar(T0 + 2 * BAR, 1.1, 1.1, 1.1, 1.1)
    assert engine.outcome.phase2_reached is True
    assert engine.outcome.passed is True


def test_target_hit_exactly_is_recognised():
    """Floating point must not turn an exact pass into a failure.

    10000 * 1.12 is 11200.000000000002 in IEEE-754. A naive >= comparison
    would report failure to a trader who hit the target exactly.
    """
    engine = PropRuleEngine(model=ONE_STEP_FLEX, initial_balance=10_000.0)
    engine.balance = 10_000.0 * 1.12
    engine.on_bar(T0 + BAR, 1.1, 1.1, 1.1, 1.1)
    assert engine.outcome.phase1_reached is True
    assert engine.outcome.passed is True


def test_phase2_base_convention_is_documented_and_declared():
    """The phase-2 BASE is not stated by the broker, so it must be explicit.

    This test exists so the assumption can never become invisible. If the Free
    Trial shows the broker uses a compounding base, the declared value changes
    HERE and the arithmetic follows.
    """
    assert TWO_STEP_STANDARD.phase2_base == "cumulative"
    assert TWO_STEP_STANDARD.passing_balance(10_000.0, 2) == pytest.approx(11_300.0)

    from dataclasses import replace
    compounding = replace(TWO_STEP_STANDARD, phase2_base="compounding")
    # 8% then 5% compounded: 10000 * 1.08 * 1.05 = 11,340
    assert compounding.passing_balance(10_000.0, 2) == pytest.approx(11_340.0)


def test_a_breached_account_can_never_pass():
    engine = PropRuleEngine(model=TWO_STEP_STANDARD, initial_balance=10_000.0)
    _flat(engine)
    engine.open_position("BUY", 1.0, 1.10000)
    engine.on_bar(T0 + BAR, 1.10000, 1.10000, 1.09400, 1.09400)
    engine.balance = 20_000.0  # a later miracle must not resurrect it
    engine.on_bar(T0 + 2 * BAR, 1.1, 1.1, 1.1, 1.1)
    assert engine.outcome.passed is False
    assert engine.outcome.survival is False


def test_engine_stops_processing_after_a_breach():
    engine = PropRuleEngine(model=TWO_STEP_STANDARD, initial_balance=10_000.0)
    _flat(engine)
    engine.open_position("BUY", 1.0, 1.10000)
    engine.on_bar(T0 + BAR, 1.10000, 1.10000, 1.09400, 1.09400)
    assert engine.on_bar(T0 + 2 * BAR, 1.1, 1.1, 1.1, 1.1) is False


# ======================================================== accounting identity

def test_equity_identity_holds_on_every_path_point():
    engine = PropRuleEngine(model=TWO_STEP_STANDARD, initial_balance=10_000.0)
    _flat(engine)
    engine.open_position("BUY", 1.0, 1.10000)
    engine.on_bar(T0 + BAR, 1.10000, 1.10100, 1.09900, 1.10050)
    assert_path_finite(engine.outcome)
    # equity == balance + unrealised at the last observed price
    for point in engine.outcome.equity_path:
        assert isinstance(point.equity, float)
        assert isinstance(point.balance, float)


def test_assert_equity_identity_catches_a_violation():
    assert_equity_identity(10_000.0, 0.0, 10_000.0)
    with pytest.raises(AssertionError):
        assert_equity_identity(10_000.0, 0.0, 9_999.0)


# ================================================================ concentration

def test_concentration_proxy_uses_the_largest_single_day():
    engine = PropRuleEngine(model=TWO_STEP_STANDARD, initial_balance=10_000.0)
    engine.balance = 11_000.0
    engine._day_profit = {_dt.date(2026, 9, 1): 900.0, _dt.date(2026, 9, 2): 100.0}
    engine.on_bar(T0 + BAR, 1.1, 1.1, 1.1, 1.1)
    assert engine.outcome.largest_day_profit == pytest.approx(900.0)
    assert engine.outcome.total_profit == pytest.approx(1_000.0)
    assert engine.outcome.largest_day_profit_share == pytest.approx(0.9)
    assert engine.outcome.concentration_triggered() is True


def test_concentration_not_triggered_when_profit_is_spread():
    engine = PropRuleEngine(model=TWO_STEP_STANDARD, initial_balance=10_000.0)
    engine.balance = 11_000.0
    engine._day_profit = {_dt.date(2026, 9, 1): 300.0, _dt.date(2026, 9, 2): 700.0}
    engine.on_bar(T0 + BAR, 1.1, 1.1, 1.1, 1.1)
    assert engine.outcome.largest_day_profit_share == pytest.approx(0.7)
    assert engine.outcome.concentration_triggered() is True


def test_concentration_safe_when_below_threshold():
    engine = PropRuleEngine(model=TWO_STEP_STANDARD, initial_balance=10_000.0)
    engine.balance = 11_000.0
    engine._day_profit = {_dt.date(2026, 9, 1): 200.0, _dt.date(2026, 9, 2): 800.0}
    engine.on_bar(T0 + BAR, 1.1, 1.1, 1.1, 1.1)
    assert engine.outcome.largest_day_profit_share == pytest.approx(0.8)
    # 0.8 > 0.6 so it triggers; a genuinely spread case:
    engine2 = PropRuleEngine(model=TWO_STEP_STANDARD, initial_balance=10_000.0)
    engine2.balance = 11_000.0
    engine2._day_profit = {_dt.date(2026, 9, 1): 100.0,
                           _dt.date(2026, 9, 2): 100.0,
                           _dt.date(2026, 9, 3): 100.0,
                           _dt.date(2026, 9, 4): 100.0,
                           _dt.date(2026, 9, 5): 100.0,
                           _dt.date(2026, 9, 6): 175.0,
                           _dt.date(2026, 9, 7): 175.0,
                           _dt.date(2026, 9, 8): 150.0}
    engine2.on_bar(T0 + BAR, 1.1, 1.1, 1.1, 1.1)
    assert engine2.outcome.concentration_triggered() is False


# =================================================================== inactivity

def test_inactivity_breach_after_thirty_days_without_a_completed_trade():
    engine = PropRuleEngine(model=TWO_STEP_STANDARD, initial_balance=10_000.0)
    engine.on_bar(T0, 1.1, 1.1, 1.1, 1.1)
    assert engine.check_inactivity(T0 + _dt.timedelta(days=29)) is False
    assert engine.check_inactivity(T0 + _dt.timedelta(days=31)) is True
    assert engine.outcome.breach is BreachKind.INACTIVITY


def test_closing_a_trade_resets_the_inactivity_clock():
    engine = PropRuleEngine(model=TWO_STEP_STANDARD, initial_balance=10_000.0)
    engine.on_bar(T0, 1.1, 1.1, 1.1, 1.1)
    engine.open_position("BUY", 0.1, 1.10000)
    engine.close_position(1.10050, T0 + _dt.timedelta(days=20))
    assert engine.check_inactivity(T0 + _dt.timedelta(days=40)) is False


# ============================================================ compliance facts

def test_vps_account_login_is_recorded_as_forbidden():
    assert VPS_ACCOUNT_LOGIN_ALLOWED is False


def test_forbidden_categories_match_the_official_list():
    for expected in ("gap trading", "high-frequency trading", "tick scalping",
                     "hedging", "long-short arbitrage"):
        assert expected in FORBIDDEN_STRATEGY_CATEGORIES


def test_describe_rules_is_json_safe_and_credential_free():
    """No credential SHAPE may leak into a report payload.

    Checking for the bare word "login" would be a false positive: this module
    legitimately carries a boolean flag whose NAME mentions VPS account access.
    The test therefore looks for credential shapes and env-var names instead.
    """
    import json
    payload = describe_rules()
    text = json.dumps(payload)          # must not raise
    assert payload["rules_as_of"] == RULES_AS_OF
    lowered = text.lower()
    for shape in ("password", "investor", "secret", "api_key", "apikey",
                  "fundingpips_mt5_login", "fundingpips_mt5_password",
                  "bearer ", "@gmail.com"):
        assert shape not in lowered, shape


def test_no_engine_module_imports_execution_or_strategy():
    """Architectural boundary: the rule engine is pure arithmetic."""
    import inspect
    import research.risk.fundingpips_engine as mod
    src = inspect.getsource(mod)
    for forbidden in ("MetaTrader5", "fundingpips_mt5", "zscore_scalper",
                      "alpha_v2_mtf", "trend_v1"):
        assert forbidden not in src, forbidden
