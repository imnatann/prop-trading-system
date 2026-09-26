"""Tests for the stage-1 readiness gate.

The gate decides whether the project may take its first irreversible action.
These tests pin both directions: it must PASS on complete evidence and BLOCK on
every individual gap, so no single missing measurement can slip through.
"""
from __future__ import annotations

import copy
import datetime as _dt
import json

import pytest

from src.execution.readiness import (
    CONNECTION_PATH,
    MAX_ACCEPTABLE_P95_SPREAD_PIPS,
    MAX_EVIDENCE_AGE_DAYS,
    MIN_SPREAD_SAMPLES,
    Check,
    ReadinessReport,
    _age_days,
    build_evidence_from_profile,
    evaluate_readiness,
    load_connection_evidence,
    load_readiness,
    save_connection_evidence,
    save_readiness,
)

UTC = _dt.timezone.utc


def good_evidence() -> dict:
    """A complete bundle: everything measured, freshly."""
    return {
        "connection": {"logged_in": True, "server_matches_expected": True,
                       "account_number_present": True},
        "symbol": {"resolved": True, "provider_symbol": "EURUSD",
                   "pip_size": 0.0001, "pip_size_positive": True,
                   "is_tradable": True},
        "spread": {"samples": 3600, "p50": 0.9, "p95": 2.1,
                   "captured_utc": _dt.datetime.now(UTC).isoformat()},
        "config": {"allow_trading": False},
        "research": {"folds": 6},
    }


def good_evidence_with_rejected_strategy() -> dict:
    """Venue fully understood, strategy comprehensively rejected.

    This is the realistic state of this project: the plumbing is ready while the
    hypothesis has failed. The gate MUST still open, because the smoke order's
    documented purpose is to validate execution, not profit.
    """
    e = good_evidence()
    e["research"] = {
        "folds": 6,
        "pairs_positive": 3,
        "pairs_total": 6,
        "pairs_required": 4,
        "falsification_verdict": "REJECTED",
        "concentration_ok": False,
    }
    return e


# ============================================================ happy path

def test_complete_evidence_is_ready():
    r = evaluate_readiness(good_evidence())
    assert r.ready is True
    assert r.blocking_failures == []


def test_every_criterion_is_reported():
    r = evaluate_readiness(good_evidence())
    keys = {c.key for c in r.checks}
    for expected in ("connection_verified", "server_is_expected",
                     "account_identified", "symbol_resolved", "pip_size_known",
                     "symbol_tradable", "spread_measured", "spread_fresh",
                     "spread_stable", "spread_sane"):
        assert expected in keys


# ================================================== every gap must block

@pytest.mark.parametrize("section,change,expected_key", [
    ("connection", {"logged_in": False}, "connection_verified"),
    ("connection", {"server_matches_expected": False}, "server_is_expected"),
    ("connection", {"account_number_present": False}, "account_identified"),
    ("symbol", {"resolved": False}, "symbol_resolved"),
    ("symbol", {"pip_size_positive": False}, "pip_size_known"),
    ("symbol", {"is_tradable": False}, "symbol_tradable"),
    ("spread", {"samples": 0}, "spread_measured"),
    ("spread", {"p95": None}, "spread_stable"),
    ("spread", {"p50": 0.0}, "spread_sane"),
    ("spread", {"captured_utc": None}, "spread_fresh"),
])
def test_a_single_gap_blocks(section, change, expected_key):
    e = good_evidence()
    e[section].update(change)
    r = evaluate_readiness(e)
    assert r.ready is False
    assert expected_key in [c.key for c in r.blocking_failures]


def test_missing_sections_block_rather_than_crash():
    r = evaluate_readiness({})
    assert r.ready is False
    assert len(r.blocking_failures) >= 10


# ================================================== thresholds are enforced

def test_spread_sample_threshold_is_enforced():
    e = good_evidence()
    e["spread"]["samples"] = MIN_SPREAD_SAMPLES - 1
    assert evaluate_readiness(e).ready is False

    e["spread"]["samples"] = MIN_SPREAD_SAMPLES
    assert evaluate_readiness(e).ready is True


def test_p95_ceiling_is_enforced():
    e = good_evidence()
    e["spread"]["p95"] = MAX_ACCEPTABLE_P95_SPREAD_PIPS + 0.01
    assert evaluate_readiness(e).ready is False

    e["spread"]["p95"] = MAX_ACCEPTABLE_P95_SPREAD_PIPS
    assert evaluate_readiness(e).ready is True


def test_stale_evidence_blocks():
    e = good_evidence()
    old = _dt.datetime.now(UTC) - _dt.timedelta(days=MAX_EVIDENCE_AGE_DAYS + 5)
    e["spread"]["captured_utc"] = old.isoformat()
    r = evaluate_readiness(e)
    assert r.ready is False
    assert "spread_fresh" in [c.key for c in r.blocking_failures]


def test_evidence_exactly_at_the_age_limit_is_still_fresh():
    e = good_evidence()
    edge = _dt.datetime.now(UTC) - _dt.timedelta(days=MAX_EVIDENCE_AGE_DAYS - 0.01)
    e["spread"]["captured_utc"] = edge.isoformat()
    assert evaluate_readiness(e).ready is True


# ================================================== warnings never block

def test_research_absence_is_only_a_warning():
    e = good_evidence()
    e["research"] = {"folds": 0}
    r = evaluate_readiness(e)
    assert r.ready is True                       # still safe to probe the venue
    assert "research_ran" in [c.key for c in r.warnings]


def test_trading_flag_on_is_a_warning_not_a_blocker():
    """The flag being on is a RISK signal, but the gate decides evidence."""
    e = good_evidence()
    e["config"]["allow_trading"] = True
    r = evaluate_readiness(e)
    assert r.ready is True
    assert "trading_flag_off" in [c.key for c in r.warnings]


# ======================================================== age helper

def test_age_days_handles_missing_and_malformed():
    assert _age_days(None) is None
    assert _age_days("not-a-date") is None


def test_age_days_accepts_zulu_suffix():
    a = _age_days("2026-01-01T00:00:00Z")
    assert a is not None and a > 0


def test_age_days_treats_naive_timestamps_as_utc():
    a = _age_days("2026-01-01T00:00:00")
    assert a is not None


# ==================================================== report behaviour

def test_report_summary_is_json_safe():
    r = evaluate_readiness(good_evidence())
    json.dumps(r.summary())          # must not raise


def test_render_lists_every_check():
    r = evaluate_readiness(good_evidence())
    text = r.render()
    for c in r.checks:
        assert c.key in text


def test_readiness_report_flags_blocking_correctly():
    rep = ReadinessReport(checks=[
        Check("a", True, "fine"),
        Check("b", False, "blocked", blocking=True),
        Check("c", False, "advisory", blocking=False),
    ])
    assert rep.ready is False
    assert [c.key for c in rep.blocking_failures] == ["b"]
    assert [c.key for c in rep.warnings] == ["c"]


# ==================================================== persistence

def test_save_then_load_round_trips(tmp_path):
    r = evaluate_readiness(good_evidence())
    target = tmp_path / "readiness.json"
    save_readiness(r, target)
    loaded = load_readiness(target)
    assert loaded is not None
    assert loaded["ready_for_smoke_order"] is True


def test_load_missing_file_returns_none(tmp_path):
    assert load_readiness(tmp_path / "nope.json") is None


def test_save_is_atomic_leaving_no_tmp_file(tmp_path):
    r = evaluate_readiness(good_evidence())
    target = tmp_path / "readiness.json"
    save_readiness(r, target)
    assert target.exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_saved_report_records_the_blocking_reasons(tmp_path):
    r = evaluate_readiness({})
    target = tmp_path / "r.json"
    save_readiness(r, target)
    payload = load_readiness(target)
    assert payload["ready_for_smoke_order"] is False
    assert len(payload["blocking_failures"]) > 0


# ============================================ evidence from real artefacts

def test_build_evidence_survives_absent_files(tmp_path):
    """No profile and no telemetry must yield MISSING evidence, never invented."""
    e = build_evidence_from_profile(
        profile_path=tmp_path / "absent_profile.json",
        connection_path=tmp_path / "absent_conn.json",
        spread_dir=tmp_path / "absent_spread",
    )
    assert e["symbol"] == {} or e["symbol"].get("resolved") in (False, None)
    assert e["spread"] == {}
    assert evaluate_readiness(e).ready is False


def test_readiness_does_not_import_mt5_at_module_level():
    """A lazy import inside a function is fine; a module-level one is not."""
    import inspect
    import src.execution.readiness as m
    tree = inspect.getsource(m)
    top_level = [ln.strip() for ln in tree.splitlines()
                 if ln and not ln.startswith((" ", "\t"))]
    for line in top_level:
        assert "MetaTrader5" not in line, line


def test_readiness_module_is_pure_over_its_input():
    """Same bundle in, same verdict out - no ambient state."""
    e = good_evidence()
    a = evaluate_readiness(copy.deepcopy(e)).ready
    b = evaluate_readiness(copy.deepcopy(e)).ready
    assert a == b


# ==================================== venue quality vs strategy quality

def test_a_rejected_strategy_does_NOT_block_a_venue_probe():
    """The separation this project depends on.

    The smoke order validates plumbing and accounting, NOT profit. Refusing an
    execution probe because the strategy is unproven would be a category error.
    """
    r = evaluate_readiness(good_evidence_with_rejected_strategy())
    assert r.ready is True
    assert r.blocking_failures == []


def test_rejected_strategy_is_still_surfaced_loudly():
    """It must not block, but it must never be silent either."""
    r = evaluate_readiness(good_evidence_with_rejected_strategy())
    warned = {c.key for c in r.warnings}
    assert "strategy_survived_falsification" in warned
    assert "profit_not_outlier_driven" in warned
    assert "cross_pair_breadth" in warned


def test_every_blocking_criterion_is_about_the_venue():
    """No strategy-quality check may ever be blocking."""
    r = evaluate_readiness(good_evidence_with_rejected_strategy())
    strategy_keys = {"strategy_survived_falsification",
                     "profit_not_outlier_driven",
                     "cross_pair_breadth", "research_ran"}
    for c in r.checks:
        if c.key in strategy_keys:
            assert c.blocking is False, (
                "%s is blocking, which conflates venue readiness with "
                "strategy quality" % c.key)


def test_outlier_driven_profit_is_reported_as_a_warning():
    e = good_evidence_with_rejected_strategy()
    e["research"]["concentration_ok"] = True
    r = evaluate_readiness(e)
    by_key = {c.key: c for c in r.checks}
    assert by_key["profit_not_outlier_driven"].passed is True

    e["research"]["concentration_ok"] = False
    r2 = evaluate_readiness(e)
    by_key2 = {c.key: c for c in r2.checks}
    assert by_key2["profit_not_outlier_driven"].passed is False


def test_breadth_threshold_is_four_of_six():
    e = good_evidence_with_rejected_strategy()
    e["research"]["pairs_positive"] = 4
    r = evaluate_readiness(e)
    by_key = {c.key: c for c in r.checks}
    assert by_key["cross_pair_breadth"].passed is True

    e["research"]["pairs_positive"] = 3
    r2 = evaluate_readiness(e)
    by_key2 = {c.key: c for c in r2.checks}
    assert by_key2["cross_pair_breadth"].passed is False


def test_absent_strategy_context_produces_no_false_reassurance():
    """If no research ran, the quality checks must be ABSENT, not passing."""
    r = evaluate_readiness(good_evidence())
    keys = {c.key for c in r.checks}
    assert "strategy_survived_falsification" not in keys
    assert "cross_pair_breadth" not in keys
    assert "profit_not_outlier_driven" not in keys


def test_venue_failure_still_blocks_even_with_perfect_strategy():
    """The converse must hold too: a good strategy cannot excuse a bad venue."""
    e = good_evidence_with_rejected_strategy()
    e["research"]["falsification_verdict"] = "SURVIVED"
    e["research"]["concentration_ok"] = True
    e["research"]["pairs_positive"] = 6
    e["spread"]["samples"] = 5           # venue evidence missing
    r = evaluate_readiness(e)
    assert r.ready is False
    assert "spread_measured" in [c.key for c in r.blocking_failures]



# ============================ connection evidence has a real producer

def test_connection_evidence_round_trips(tmp_path):
    """Before this existed the three connection criteria could NEVER pass."""
    target = tmp_path / "conn.json"
    save_connection_evidence(login=40000001234, server="FundingPips-Trial",
                             expected_server="FundingPips-Trial", path=target)
    data = load_connection_evidence(target)
    assert data["logged_in"] is True
    assert data["server_matches_expected"] is True
    assert data["account_number_present"] is True


def test_connection_evidence_never_stores_the_full_account(tmp_path):
    target = tmp_path / "conn.json"
    save_connection_evidence(40000001234, "FundingPips-Trial",
                             "FundingPips-Trial", path=target)
    raw = target.read_text()
    assert "40000001234" not in raw, "the full login must never be persisted"
    assert "password" not in raw.lower()


def test_connection_evidence_detects_a_server_mismatch(tmp_path):
    target = tmp_path / "conn.json"
    save_connection_evidence(12345678, "OtherBroker-Demo",
                             "FundingPips-Trial", path=target)
    data = load_connection_evidence(target)
    assert data["server_matches_expected"] is False


def test_missing_connection_evidence_reads_as_empty(tmp_path):
    assert load_connection_evidence(tmp_path / "nope.json") == {}


def test_corrupt_connection_evidence_does_not_crash(tmp_path):
    target = tmp_path / "conn.json"
    target.write_text("{ not json")
    assert load_connection_evidence(target) == {}


def test_connection_criteria_pass_once_evidence_exists(tmp_path):
    """The exact bug this round fixed: a criterion with no producer."""
    target = tmp_path / "conn.json"
    save_connection_evidence(40000001234, "FundingPips-Trial",
                             "FundingPips-Trial", path=target)
    ev = build_evidence_from_profile(connection_path=target)
    r = evaluate_readiness(ev)
    failed = [c.key for c in r.blocking_failures]
    assert "connection_verified" not in failed
    assert "server_is_expected" not in failed
    assert "account_identified" not in failed


def test_connection_criteria_fail_without_evidence(tmp_path):
    ev = build_evidence_from_profile(connection_path=tmp_path / "nope.json")
    r = evaluate_readiness(ev)
    failed = [c.key for c in r.blocking_failures]
    assert "connection_verified" in failed
    assert "server_is_expected" in failed
    assert "account_identified" in failed


def test_explicit_connection_argument_overrides_the_file(tmp_path):
    target = tmp_path / "conn.json"
    save_connection_evidence(40000001234, "FundingPips-Trial",
                             "FundingPips-Trial", path=target)
    ev = build_evidence_from_profile(
        connection={"logged_in": False, "server_matches_expected": False,
                    "account_number_present": False},
        connection_path=target)
    assert ev["connection"]["logged_in"] is False


def test_the_whole_gate_opens_from_artefacts_alone(tmp_path, monkeypatch):
    """The integration proof: no hand-injected evidence anywhere.

    Writes exactly the files the three stage-1 tools produce, points the
    loader at them, and requires all ten blocking criteria to pass.
    """
    import datetime as _dt
    import json

    from src.execution.models import SymbolInfo, TradeMode

    root = tmp_path
    execdir = root / "data" / "execution" / "fundingpips"
    (execdir / "canonical").mkdir(parents=True)

    save_connection_evidence(40000001234, "FundingPips-Trial",
                             "FundingPips-Trial",
                             path=execdir / "connection_evidence.json")

    si = SymbolInfo(
        canonical_symbol="EURUSD", provider_symbol="EURUSD",
        digits=5, point=0.00001, trade_tick_size=0.00001,
        trade_tick_value=1.0, contract_size=100000.0, volume_min=0.01,
        volume_max=100.0, volume_step=0.01, stops_level=0, freeze_level=0,
        trade_mode=TradeMode.FULL, swap_long=-10.0, swap_short=-5.0,
        swap_mode=1, swap_rollover3days=3, currency_base="EUR",
        currency_profit="USD", currency_margin="EUR",
        execution_mode="MARKET", filling_modes=1, spread=10,
        visible=True, select_ok=True)
    profile = {"schema_version": "1", "canonical_symbol": "EURUSD",
               "provider_symbol": "EURUSD", "symbol": si.to_dict(),
               "captured_at_utc": _dt.datetime.now(_dt.timezone.utc).isoformat()}
    (execdir / "execution_profile.json").write_text(json.dumps(profile))

    now = _dt.datetime.now(_dt.timezone.utc)
    with (execdir / "canonical" / "EURUSD_spread.jsonl").open("w") as fh:
        for i in range(3600):
            fh.write(json.dumps({
                "timestamp_utc": (now - _dt.timedelta(seconds=3600 - i)).isoformat(),
                "spread_pips": 0.8 + (i % 7) * 0.1}) + "\n")

    # Pass every path EXPLICITLY. Relying on the default would resolve against
    # the module-level path, which is anchored to the repo root -- so a test
    # could write into the REAL evidence directory. That happened once and the
    # live readiness check reported three criteria PASSED with no MT5 login.
    monkeypatch.chdir(root)
    ev = build_evidence_from_profile(
        connection_path=execdir / "connection_evidence.json",
        spread_dir=execdir / "canonical",
        profile_path=execdir / "execution_profile.json")
    r = evaluate_readiness(ev)
    assert r.ready is True, [c.key for c in r.blocking_failures]
    assert len([c for c in r.checks if c.blocking]) == 10
