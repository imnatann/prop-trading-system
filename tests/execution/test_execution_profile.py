"""Execution profile tests.

Covers required cases 10, 11, 22 and the profile -> simulator_v2 conversion.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.execution.execution_profile import (
    FillObservation,
    FundingPipsExecutionProfile,
    SpreadStats,
    build_profile,
)
from src.execution.models import AccountInfo, SimulationCostParameters, TradeMode
from src.execution.telemetry import SpreadRecorder, SpreadSample
from tests.execution.fake_mt5 import FakeAccount, FakeMetaTrader5, FakeSymbol, fakes_env
from src.execution.fundingpips_mt5 import FundingPipsMT5Adapter


# --------------------------------------------- case 10: SymbolInfo mapping
def test_symbol_info_mapping_from_mt5_metadata():
    fake = FakeMetaTrader5(symbols=[FakeSymbol(name="EURUSD.r", digits=5, swap_long=-8.4, swap_short=2.1)])
    adapter = FundingPipsMT5Adapter.from_env(env=fakes_env(), mt5_module=fake)
    adapter.connect()
    info = adapter.symbol_info("EURUSD")

    assert info.canonical_symbol == "EURUSD"
    assert info.provider_symbol == "EURUSD.r"
    assert info.digits == 5
    assert info.point == 0.00001
    assert info.contract_size == 100_000.0
    assert info.volume_min == 0.01
    assert info.volume_step == 0.01
    assert info.trade_mode is TradeMode.FULL
    assert info.swap_long == -8.4
    assert info.swap_short == 2.1
    assert info.currency_base == "EUR"
    assert info.currency_profit == "USD"


def test_symbol_info_does_not_assume_pip_size_for_jpy():
    """A 3-digit JPY quote derives a 0.01 pip, not the EURUSD 0.0001."""
    fake = FakeMetaTrader5(symbols=[FakeSymbol(name="USDJPY", digits=3, point=0.001, trade_tick_size=0.001)])
    adapter = FundingPipsMT5Adapter.from_env(env=fakes_env(), mt5_module=fake)
    adapter.connect()
    info = adapter.symbol_info("USDJPY")
    assert info.pip_size == pytest.approx(0.01)


# ------------------------------------------------ case 11: pip size calc
@pytest.mark.parametrize("digits,point,expected", [
    (5, 0.00001, 0.0001),
    (3, 0.001, 0.01),
])
def test_pip_size_derived_from_metadata(digits, point, expected):
    fake = FakeMetaTrader5(symbols=[FakeSymbol(name="EURUSD", digits=digits, point=point)])
    adapter = FundingPipsMT5Adapter.from_env(env=fakes_env(), mt5_module=fake)
    adapter.connect()
    assert adapter.symbol_info("EURUSD").pip_size == pytest.approx(expected)


def test_pip_size_is_not_hardcoded_for_two_digit_symbols():
    """2-digit instruments use pip == point instead of the FX x10 convention."""
    fake = FakeMetaTrader5(symbols=[FakeSymbol(name="XAUUSD", digits=2, point=0.01)])
    adapter = FundingPipsMT5Adapter.from_env(env=fakes_env(), mt5_module=fake)
    adapter.connect()
    assert adapter.symbol_info("XAUUSD").pip_size == pytest.approx(0.01)


def test_declared_spread_converts_to_pips():
    fake = FakeMetaTrader5(symbols=[FakeSymbol(name="EURUSD", spread=12, point=0.00001)])
    adapter = FundingPipsMT5Adapter.from_env(env=fakes_env(), mt5_module=fake)
    adapter.connect()
    info = adapter.symbol_info("EURUSD")
    assert info.spread_pips == pytest.approx(1.2)


# ------------------------------------------------ profile construction
def test_build_profile_from_symbol_info():
    fake = FakeMetaTrader5(symbols=[FakeSymbol(name="EURUSD", swap_long=-8.0, swap_short=2.0)])
    adapter = FundingPipsMT5Adapter.from_env(env=fakes_env(), mt5_module=fake)
    adapter.connect()
    profile = build_profile(adapter.symbol_info("EURUSD"), account=adapter.account_info())

    assert profile.canonical_symbol == "EURUSD"
    assert profile.provider_symbol == "EURUSD"
    assert profile.pip_size == pytest.approx(0.0001)
    assert profile.swap_long_pips == pytest.approx(-0.8)
    assert profile.swap_short_pips == pytest.approx(0.2)


def test_profile_swap_conversion_uses_derived_pip():
    """Swap is quoted in points; the conversion must use the DERIVED pip size."""
    fake = FakeMetaTrader5(symbols=[FakeSymbol(name="USDJPY", digits=3, point=0.001, swap_long=-100.0)])
    adapter = FundingPipsMT5Adapter.from_env(env=fakes_env(), mt5_module=fake)
    adapter.connect()
    info = adapter.symbol_info("USDJPY")
    profile = build_profile(info)
    # -100 points * 0.001 = -0.1 price units; / 0.01 pip = -10 pips
    assert profile.swap_long_pips == pytest.approx(-10.0)


def test_profile_to_dict_is_credential_free():
    fake = FakeMetaTrader5(symbols=[FakeSymbol(name="EURUSD")])
    adapter = FundingPipsMT5Adapter.from_env(env=fakes_env(password="never-leak"), mt5_module=fake)
    adapter.connect()
    profile = build_profile(adapter.symbol_info("EURUSD"), account=adapter.account_info())
    payload = profile.to_dict()

    assert "never-leak" not in str(payload)
    assert "password" not in str(payload)
    assert payload["account"]["login_masked"] == "****5678"
    assert "12345678" not in str(payload)


# ------------------------------------- profile -> SimulationCostParameters
def test_profile_to_simulation_costs_defaults_to_median():
    stats = SpreadStats(samples=100, p50=1.1, p75=1.5, p90=2.0, p95=2.4, p99=3.9, mean=1.3)
    fake = FakeMetaTrader5()
    adapter = FundingPipsMT5Adapter.from_env(env=fakes_env(), mt5_module=fake)
    adapter.connect()
    profile = build_profile(adapter.symbol_info("EURUSD"), spread_stats=stats)

    params = profile.to_simulation_costs()
    assert isinstance(params, SimulationCostParameters)
    assert params.spread_pips == pytest.approx(1.1)
    assert params.samples == 100
    assert params.spread_p90 == pytest.approx(2.0)


def test_profile_to_simulation_costs_selects_requested_quantile():
    stats = SpreadStats(samples=50, p50=1.0, p90=3.0, p95=4.0, mean=1.5)
    profile = FundingPipsExecutionProfile(
        canonical_symbol="EURUSD",
        provider_symbol="EURUSD",
        symbol=_simple_symbol_info(),
        spread_stats=stats,
    )
    assert profile.to_simulation_costs(spread_quantile="p90").spread_pips == pytest.approx(3.0)
    assert profile.to_simulation_costs(spread_quantile="p95").spread_pips == pytest.approx(4.0)


def test_quantile_selection_is_independent_of_simulator():
    """The conversion must not import or mutate simulator_v2."""
    import inspect

    import research.execution.calibrate_fundingpips as calib

    source = inspect.getsource(calib)
    assert "import simulator_v2" not in source
    assert "from research.backtest" not in source


def test_profile_without_telemetry_records_its_weakness():
    """With no observed samples the profile must say so rather than look precise."""
    profile = FundingPipsExecutionProfile(
        canonical_symbol="EURUSD",
        provider_symbol="EURUSD",
        symbol=_simple_symbol_info(),
    )
    params = profile.to_simulation_costs()
    assert params.samples == 0
    assert "no_spread_telemetry" in params.notes
    assert params.slippage_pips == 0.0


def test_observed_slippage_aggregates_fills():
    profile = FundingPipsExecutionProfile(
        canonical_symbol="EURUSD",
        provider_symbol="EURUSD",
        symbol=_simple_symbol_info(),
        fills=[
            FillObservation(requested_price=1.0, filled_price=1.00002, side="BUY", volume=0.01, slippage_pips=0.2),
            FillObservation(requested_price=1.0, filled_price=1.00004, side="BUY", volume=0.01, slippage_pips=0.4),
        ],
    )
    assert profile.observed_slippage_pips == pytest.approx(0.3)
    params = profile.to_simulation_costs()
    assert params.slippage_pips == pytest.approx(0.3)


def test_profile_round_trip_through_disk(tmp_path):
    fake = FakeMetaTrader5()
    adapter = FundingPipsMT5Adapter.from_env(env=fakes_env(), mt5_module=fake)
    adapter.connect()
    profile = build_profile(adapter.symbol_info("EURUSD"), account=adapter.account_info())
    target = profile.save(tmp_path / "profile.json")

    import json

    loaded = json.loads(target.read_text())
    assert loaded["canonical_symbol"] == "EURUSD"
    assert loaded["symbol"]["provider_symbol"] == "EURUSD"


def _simple_symbol_info():
    from src.execution.models import SymbolInfo

    return SymbolInfo(
        canonical_symbol="EURUSD",
        provider_symbol="EURUSD",
        digits=5,
        point=0.00001,
        trade_tick_size=0.00001,
        trade_tick_value=1.0,
        contract_size=100_000.0,
        volume_min=0.01,
        volume_max=50.0,
        volume_step=0.01,
        trade_mode=TradeMode.FULL,
    )

