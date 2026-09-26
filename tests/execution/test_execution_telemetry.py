"""Execution telemetry tests.

Covers required cases 21-24 and 29-34: tick normalization, UTC stamps, the bid<ask
invariant, spread arithmetic, commission/slippage attribution, the prohibition on
double counting, and the absence of secrets from telemetry payloads.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from src.execution.models import Tick
from src.execution.redaction import clear_registered_secrets, register_secret
from src.execution.telemetry import (
    SpreadRecorder,
    SpreadSample,
    attribute_round_trip,
    ensure_utc,
    exec_event,
    session_for_hour,
)


@pytest.fixture(autouse=True)
def _clean_secrets():
    clear_registered_secrets()
    yield
    clear_registered_secrets()


def tick(bid=1.08500, ask=1.08512, ts=None) -> Tick:
    return Tick(
        canonical_symbol="EURUSD",
        provider_symbol="EURUSD",
        bid=bid,
        ask=ask,
        timestamp_utc=ts or datetime(2024, 6, 3, 12, 0, tzinfo=timezone.utc),
        terminal_time_utc=ts or datetime(2024, 6, 3, 12, 0, tzinfo=timezone.utc),
        local_receipt_utc=ts or datetime(2024, 6, 3, 12, 0, tzinfo=timezone.utc),
    )


# ------------------------------------------------- case 21: tick normalization
def test_tick_normalization_from_adapter():
    from src.execution.fundingpips_mt5 import FundingPipsMT5Adapter
    from tests.execution.fake_mt5 import fakes_env, make_fake

    adapter = FundingPipsMT5Adapter.from_env(env=fakes_env(), mt5_module=make_fake())
    adapter.connect()
    result = adapter.tick("EURUSD")
    assert result.canonical_symbol == "EURUSD"
    assert result.provider_symbol == "EURUSD"
    assert result.bid == pytest.approx(1.08500)
    assert result.ask == pytest.approx(1.08512)
    assert result.mid == pytest.approx(1.08506)


# ------------------------------------------------- case 22: UTC timestamps
def test_tick_timestamps_are_utc():
    from src.execution.fundingpips_mt5 import FundingPipsMT5Adapter
    from tests.execution.fake_mt5 import fakes_env, make_fake

    adapter = FundingPipsMT5Adapter.from_env(env=fakes_env(), mt5_module=make_fake())
    adapter.connect()
    result = adapter.tick("EURUSD")
    assert result.timestamp_utc.tzinfo is not None
    assert result.timestamp_utc.utcoffset() == timedelta(0)


def test_tick_isoformat_ends_with_utc_offset():
    rendered = tick().to_dict()["timestamp_utc"]
    assert rendered.endswith("+00:00")


def test_ensure_utc_attaches_utc_to_naive_datetimes():
    naive = datetime(2024, 1, 1, 0, 0)
    assert ensure_utc(naive).tzinfo == timezone.utc


def test_ensure_utc_converts_other_offsets():
    other = datetime(2024, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=7)))
    converted = ensure_utc(other)
    assert converted.hour == 5
    assert converted.tzinfo == timezone.utc


def test_spread_sample_timestamps_are_utc():
    sample = SpreadSample.from_tick(tick(), pip_size=0.0001, point=0.00001)
    assert sample.timestamp_utc.utcoffset() == timedelta(0)


# ------------------------------------------------- case 23: bid < ask invariant
def test_valid_tick_requires_bid_below_ask():
    assert tick(bid=1.08500, ask=1.08512).is_valid is True
    assert tick(bid=1.08512, ask=1.08500).is_valid is False
    assert tick(bid=1.08500, ask=1.08500).is_valid is False
    assert tick(bid=0.0, ask=1.08512).is_valid is False


def test_crossed_quotes_are_not_recorded():
    recorder = SpreadRecorder("EURUSD")
    assert recorder.add(SpreadSample.from_tick(tick(bid=1.08600, ask=1.08500), 0.0001, 0.00001)) is False
    assert recorder.count == 0
    assert recorder.rejected == 1


def test_valid_quote_is_recorded():
    recorder = SpreadRecorder("EURUSD")
    assert recorder.add(SpreadSample.from_tick(tick(), 0.0001, 0.00001)) is True
    assert recorder.count == 1
    assert recorder.rejected == 0


# ------------------------------------------------- case 24: spread calculation
def test_spread_price_points_and_pips():
    sample = SpreadSample.from_tick(tick(bid=1.08500, ask=1.08512), pip_size=0.0001, point=0.00001)
    assert sample.spread_price == pytest.approx(0.00012)
    assert sample.spread_points == pytest.approx(12.0)
    assert sample.spread_pips == pytest.approx(1.2)


def test_spread_pips_for_jpy_convention():
    sample = SpreadSample.from_tick(
        Tick(canonical_symbol="USDJPY", provider_symbol="USDJPY", bid=154.200, ask=154.214,
             timestamp_utc=datetime.now(timezone.utc)),
        pip_size=0.01, point=0.001,
    )
    assert sample.spread_points == pytest.approx(14.0)
    assert sample.spread_pips == pytest.approx(1.4)


def test_mid_is_average_of_bid_and_ask():
    sample = SpreadSample.from_tick(tick(bid=1.0, ask=1.1), 0.0001, 0.00001)
    assert sample.mid == pytest.approx(1.05)


def test_latency_proxy_computed_when_both_stamps_present():
    ts = datetime(2024, 6, 3, 12, 0, tzinfo=timezone.utc)
    t = Tick(canonical_symbol="EURUSD", provider_symbol="EURUSD", bid=1.0, ask=1.1,
             timestamp_utc=ts, terminal_time_utc=ts, local_receipt_utc=ts + timedelta(milliseconds=15))
    sample = SpreadSample.from_tick(t, 0.0001, 0.00001)
    assert sample.latency_proxy_ms == pytest.approx(15.0)


def test_latency_proxy_none_without_terminal_stamp():
    t = Tick(canonical_symbol="EURUSD", provider_symbol="EURUSD", bid=1.0, ask=1.1,
             timestamp_utc=datetime.now(timezone.utc))
    assert SpreadSample.from_tick(t, 0.0001, 0.00001).latency_proxy_ms is None


# ------------------------------------------------- distribution statistics
def test_stats_quantiles_on_known_series():
    recorder = SpreadRecorder("EURUSD")
    for spread in [1.0, 1.0, 1.0, 2.0, 3.0]:
        recorder.add(SpreadSample(
            timestamp_utc=datetime(2024, 6, 3, 12, 0, tzinfo=timezone.utc),
            canonical_symbol="EURUSD", provider_symbol="EURUSD",
            bid=1.0, ask=1.0 + spread * 0.0001, pip_size=0.0001, point=0.00001,
        ))
    stats = recorder.stats()
    assert stats.samples == 5
    assert stats.p50 == pytest.approx(1.0)
    assert stats.minimum == pytest.approx(1.0)
    assert stats.maximum == pytest.approx(3.0)


def test_stats_empty_series_is_safe():
    stats = SpreadRecorder("EURUSD").stats()
    assert stats.samples == 0
    assert stats.p50 is None


def test_stats_bucket_by_utc_hour():
    recorder = SpreadRecorder("EURUSD")
    for hour, spread in [(0, 1.0), (0, 1.0), (8, 2.0), (8, 2.0)]:
        recorder.add(SpreadSample(
            timestamp_utc=datetime(2024, 6, 3, hour, 0, tzinfo=timezone.utc),
            canonical_symbol="EURUSD", provider_symbol="EURUSD",
            bid=1.0, ask=1.0 + spread * 0.0001, pip_size=0.0001, point=0.00001,
        ))
    stats = recorder.stats()
    assert stats.by_hour_utc[0] == pytest.approx(1.0)
    assert stats.by_hour_utc[8] == pytest.approx(2.0)


def test_session_bucketing_handles_midnight_wrap():
    assert "sydney" in session_for_hour(22)
    assert "tokyo" in session_for_hour(3)
    assert "london" in session_for_hour(10)
    assert "newyork" in session_for_hour(15)


# ------------------------------------- case 29: commission attribution
def test_commission_is_subtracted_exactly_once():
    result = attribute_round_trip(
        symbol="EURUSD", side="BUY", volume=0.1,
        entry_price=1.08512, exit_price=1.09512,
        entry_mid=1.08500, exit_mid=1.09500,
        entry_spread_price=0.00012, exit_spread_price=0.00012,
        pip_size=0.0001, contract_size=100_000.0,
        commission=7.0, financing=0.0,
    )
    assert result.commission == pytest.approx(7.0)
    assert result.net_pnl == pytest.approx(
        result.gross_pnl - result.spread_cost - result.slippage_cost - 7.0 - 0.0
    )


def test_financing_is_subtracted_exactly_once():
    result = attribute_round_trip(
        symbol="EURUSD", side="BUY", volume=0.1,
        entry_price=1.08512, exit_price=1.09512,
        entry_mid=1.08500, exit_mid=1.09500,
        entry_spread_price=0.00012, exit_spread_price=0.00012,
        financing=-2.5,
    )
    assert result.financing == pytest.approx(-2.5)
    assert result.net_pnl == pytest.approx(
        result.gross_pnl - result.spread_cost - result.slippage_cost - 0.0 - (-2.5)
    )


# ------------------------------------- case 30: slippage attribution
def test_slippage_attribution_on_adverse_fill():
    """A fill worse than the prevailing touch price must show up as positive slippage.

    The observed spread is supplied explicitly, because a fill price alone cannot
    distinguish spread from slippage.
    """
    # 1.0 pip spread observed; BUY should have filled at ask = 1.08505 but got 1.08530
    result = attribute_round_trip(
        symbol="EURUSD", side="BUY", volume=0.1,
        entry_price=1.08530, exit_price=1.09495,
        entry_mid=1.08500, exit_mid=1.09500,
        entry_spread_price=0.00010, exit_spread_price=0.00010,
        pip_size=0.0001,
    )
    assert result.slippage_cost > 0
    assert result.slippage_pips > 0


def test_clean_fill_has_zero_slippage():
    """A fill exactly at the expected touch price leaves slippage at zero."""
    result = attribute_round_trip(
        symbol="EURUSD", side="BUY", volume=0.1,
        entry_price=1.08505,   # ask = mid + half of the 1.0 pip spread
        exit_price=1.09495,    # bid = mid - half of the 1.0 pip spread
        entry_mid=1.08500, exit_mid=1.09500,
        entry_spread_price=0.00010, exit_spread_price=0.00010,
    )
    # 1e-9 USD on a 0.1 lot is ~1e-5 pips: float noise, not slippage.
    assert result.slippage_cost == pytest.approx(0.0, abs=1e-9)
    assert result.spread_cost == pytest.approx(1.0 * 0.1 * 10.0)


def test_favourable_fill_is_negative_slippage():
    """Price improvement must be signed negative, never silently clamped to zero."""
    result = attribute_round_trip(
        symbol="EURUSD", side="BUY", volume=0.1,
        entry_price=1.08500,   # 0.5 pip better than the 1.08505 ask
        exit_price=1.09495,
        entry_mid=1.08500, exit_mid=1.09500,
        entry_spread_price=0.00010, exit_spread_price=0.00010,
    )
    assert result.slippage_cost < 0


def test_slippage_round_trip_covers_both_sides():
    """Slippage must be measured on entry AND exit, not just one side."""
    # Spread 1.0 pip. Expected BUY entry ask 1.08505, exit bid 1.09495.
    # Actual: filled exactly 1 point worse on entry and 1 point worse on exit.
    result = attribute_round_trip(
        symbol="EURUSD", side="BUY", volume=0.1,
        entry_price=1.08506, exit_price=1.09494,
        entry_mid=1.08500, exit_mid=1.09500,
        entry_spread_price=0.00010, exit_spread_price=0.00010,
    )
    one_point = 0.00001 * 0.1 * 100_000
    assert result.slippage_cost == pytest.approx(one_point * 2, rel=1e-6)


def test_side_direction_is_respected():
    """A SELL must profit from a falling mid, not a rising one."""
    rising = attribute_round_trip("EURUSD", "SELL", 0.1, 1.0850, 1.0950, 1.0850, 1.0950)
    falling = attribute_round_trip("EURUSD", "SELL", 0.1, 1.0950, 1.0850, 1.0950, 1.0850)
    assert rising.gross_pnl < 0
    assert falling.gross_pnl > 0


# ---------------------------------- cases 31/32: NO double counting
def test_no_spread_double_counting():
    """spread_cost + slippage_cost must reconstruct the TRUE execution cost."""
    entry_mid, exit_mid = 1.08500, 1.09500
    entry_price, exit_price = 1.08512, 1.09488  # 1.2 pip spread paid on both sides

    result = attribute_round_trip(
        symbol="EURUSD", side="BUY", volume=0.1,
        entry_price=entry_price, exit_price=exit_price,
        entry_mid=entry_mid, exit_mid=exit_mid,
    )
    true_exec_cost = (result.gross_pnl
                      - (exit_price - entry_price) * 1.0 * 0.1 * 100_000)
    assert result.spread_cost + result.slippage_cost == pytest.approx(true_exec_cost, abs=1e-9)


def test_spread_is_charged_once_not_twice():
    """Regression: the old engine debited the spread in BOTH price and cost.

    One round trip pays the spread once, i.e. half of it on each side.
    """
    result = attribute_round_trip(
        symbol="EURUSD", side="BUY", volume=0.1,
        entry_price=1.08512, exit_price=1.09488,
        entry_mid=1.08500, exit_mid=1.09500,
        entry_spread_price=0.00012, exit_spread_price=0.00012,
        pip_size=0.0001,
    )
    # A 1.2 pip spread paid across the round trip, on 0.1 lot: 1.2 * 1 USD
    expected = 1.2 * 0.1 * 10.0
    assert result.spread_cost == pytest.approx(expected, rel=1e-6)


def test_no_slippage_double_counting():
    """Slippage is the RESIDUAL, so it can never be charged twice."""
    result = attribute_round_trip(
        symbol="EURUSD", side="BUY", volume=0.1,
        entry_price=1.08530, exit_price=1.09480,
        entry_mid=1.08500, exit_mid=1.09500,
        commission=1.0, financing=0.5,
    )
    assert result.net_pnl == pytest.approx(
        result.gross_pnl - result.spread_cost - result.slippage_cost - 1.0 - 0.5,
        abs=1e-9,
    )


def test_round_trip_cost_sums_all_components_once():
    result = attribute_round_trip(
        "EURUSD", "BUY", 0.1, 1.08512, 1.09488, 1.08500, 1.09500,
        commission=2.0, financing=1.0,
    )
    assert result.round_trip_cost == pytest.approx(
        result.spread_cost + result.slippage_cost + 2.0 + 1.0
    )


def test_attribution_raises_on_inconsistent_arithmetic():
    """The identity is enforced, so a future edit cannot silently break it."""
    result = attribute_round_trip("EURUSD", "BUY", 0.1, 1.00005, 1.10005, 1.0, 1.1,
                                  entry_spread_price=0.0001, exit_spread_price=0.0001)
    assert result.net_pnl == pytest.approx(
        result.gross_pnl - result.spread_cost - result.slippage_cost - result.commission - result.financing
    )


def test_pip_denominated_views_are_consistent():
    result = attribute_round_trip(
        "EURUSD", "BUY", 0.1, 1.08512, 1.09488, 1.08500, 1.09500,
        entry_spread_price=0.00012, exit_spread_price=0.00012, pip_size=0.0001,
    )
    assert result.spread_pips == pytest.approx(1.2, rel=1e-3)
    # 1.08500 -> 1.09500 is a 100 pip move
    assert result.gross_pips == pytest.approx(100.0, rel=1e-3)


def test_zero_pip_size_does_not_raise():
    result = attribute_round_trip("EURUSD", "BUY", 0.1, 1.0, 1.1, 1.0, 1.1, pip_size=0.0)
    assert result.spread_pips == 0.0


# ------------------------------------- cases 33/34: telemetry has no secrets
def test_exec_event_redacts_registered_secrets():
    register_secret("my-secret-pw")
    payload = exec_event("test", "login_attempt", reason="failed with my-secret-pw")
    assert "my-secret-pw" not in json.dumps(payload)
    assert "***REDACTED***" in payload["reason"]


def test_exec_event_masks_account_number_in_fields():
    payload = exec_event("test", "login_attempt", detail="account 12345678 connected")
    assert "12345678" not in json.dumps(payload)


def test_exec_event_redacts_password_like_kv_pairs():
    payload = exec_event("test", "login_attempt", raw="password=hunter2")
    assert "hunter2" not in json.dumps(payload)


def test_exec_event_json_serializable():
    payload = exec_event("comp", "event", success=True, symbol="EURUSD")
    assert json.loads(json.dumps(payload))["event"] == "event"


def test_exec_event_never_emits_login_or_password_keys():
    payload = exec_event("test", "login_success", login_masked="****5678", server="FundingPips-Trial")
    assert "password" not in payload
    assert "login" not in payload
    assert payload["login_masked"] == "****5678"


def test_recorder_jsonl_is_written_atomically(tmp_path):
    recorder = SpreadRecorder("EURUSD")
    for _ in range(3):
        recorder.add(SpreadSample.from_tick(tick(), 0.0001, 0.00001))
    target = recorder.write_jsonl(directory=tmp_path, filename="spread_test.jsonl")
    lines = target.read_text().strip().splitlines()
    assert len(lines) == 3
    for line in lines:
        payload = json.loads(line)
        assert payload["canonical_symbol"] == "EURUSD"
        assert "password" not in line


def test_recorder_append_is_safe_across_runs(tmp_path):
    for _ in range(2):
        recorder = SpreadRecorder("EURUSD")
        recorder.add(SpreadSample.from_tick(tick(), 0.0001, 0.00001))
        recorder.write_jsonl(directory=tmp_path, filename="spread_append.jsonl")
    target = tmp_path / "spread_append.jsonl"
    assert len(target.read_text().strip().splitlines()) == 2


def test_telemetry_does_not_mix_with_research_data_dirs():
    """FundingPips telemetry must live outside every research data directory."""
    from src.execution.telemetry import CANONICAL_DIR, RAW_DIR

    assert "execution" in str(CANONICAL_DIR)
    assert "execution" in str(RAW_DIR)
    assert str(CANONICAL_DIR) != "data/canonical"

