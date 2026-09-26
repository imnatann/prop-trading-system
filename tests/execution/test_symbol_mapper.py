"""Symbol resolution tests.

Covers required cases 7, 8, 9: exact resolution, suffix resolution, and ambiguous
resolution failing loudly.
"""
from __future__ import annotations

import pytest

from src.execution.errors import AmbiguousSymbolError, SymbolNotFoundError
from src.execution.symbol_mapper import (
    SymbolResolution,
    available_symbol_names,
    resolve_symbol,
)
from tests.execution.fake_mt5 import FakeMetaTrader5, FakeSymbol


BROKER_SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "XAUUSD"]


# ------------------------------------------------- case 7: exact resolution
def test_exact_symbol_resolves():
    result = resolve_symbol("EURUSD", BROKER_SYMBOLS)
    assert result.provider_symbol == "EURUSD"
    assert result.canonical_symbol == "EURUSD"
    assert result.match_rule == "exact"


def test_exact_match_wins_over_suffixed_variants():
    """Exact must beat a suffix candidate even when both exist."""
    result = resolve_symbol("EURUSD", ["EURUSD", "EURUSD.r", "EURUSD_raw"])
    assert result.provider_symbol == "EURUSD"
    assert result.match_rule == "exact"


def test_resolution_is_case_insensitive():
    assert resolve_symbol("eurusd", ["EURUSD"]).provider_symbol == "EURUSD"
    assert resolve_symbol("EURUSD", ["eurusd"]).provider_symbol == "eurusd"


# ------------------------------------------------- case 8: suffix resolution
@pytest.mark.parametrize("suffix", [".r", ".raw", ".a", ".pro", ".ecn", "_raw", "_ecn", "m"])
def test_suffixed_symbol_resolves(suffix):
    provider = "EURUSD" + suffix
    result = resolve_symbol("EURUSD", ["GBPUSD", provider])
    assert result.provider_symbol == provider
    assert result.canonical_symbol == "EURUSD"
    assert result.match_rule in ("canonical.dotted_suffix", "canonical.prefix", "known_variant")


def test_suffix_resolution_is_deterministic():
    """The SAME ambiguity must produce the SAME diagnostic on every call.

    Two dotted variants of the canonical symbol genuinely tie at the same rank, so
    both calls must refuse - deterministically - rather than the second call quietly
    picking one.
    """
    symbols = ["EURUSD.r", "EURUSD.a"]
    messages = []
    for _ in range(3):
        with pytest.raises(AmbiguousSymbolError) as excinfo:
            resolve_symbol("EURUSD", symbols)
        messages.append(str(excinfo.value))
    assert len(set(messages)) == 1, "ambiguity diagnosis must be stable"


def test_separator_variant_resolves():
    result = resolve_symbol("EURUSD", ["EUR/USD", "GBPUSD"])
    assert result.provider_symbol == "EUR/USD"


def test_prefix_variant_resolves():
    result = resolve_symbol("EURUSD", ["#EURUSD", "GBPUSD"])
    assert result.provider_symbol == "#EURUSD"


# ------------------------------------------------- case 9: ambiguity fails
def test_ambiguous_symbol_fails():
    with pytest.raises(AmbiguousSymbolError) as excinfo:
        resolve_symbol("EURUSD", ["EURUSD.a", "EURUSD.b"])
    message = str(excinfo.value)
    assert "EURUSD.a" in message and "EURUSD.b" in message


def test_ambiguity_reports_all_candidates():
    with pytest.raises(AmbiguousSymbolError) as excinfo:
        resolve_symbol("EURUSD", ["EURUSD.one", "EURUSD.two", "EURUSD.three"])
    assert len(excinfo.value.context.get("candidates", [])) == 3


def test_ambiguous_never_silently_selects():
    """Resolution must never return when two candidates tie."""
    for _ in range(5):
        with pytest.raises(AmbiguousSymbolError):
            resolve_symbol("EURUSD", ["EURUSD.x", "EURUSD.y"])


def test_no_candidate_fails():
    with pytest.raises(SymbolNotFoundError):
        resolve_symbol("EURUSD", ["GBPUSD", "USDJPY"])


def test_empty_symbol_list_fails():
    with pytest.raises(SymbolNotFoundError):
        resolve_symbol("EURUSD", [])


def test_empty_canonical_fails():
    with pytest.raises(SymbolNotFoundError):
        resolve_symbol("", BROKER_SYMBOLS)


def test_resolution_to_dict_is_json_safe():
    import json

    payload = resolve_symbol("EURUSD", BROKER_SYMBOLS).to_dict()
    assert json.loads(json.dumps(payload))["provider_symbol"] == "EURUSD"


def test_available_symbol_names_reads_only():
    fake = FakeMetaTrader5(symbols=[FakeSymbol(name="EURUSD"), FakeSymbol(name="GBPUSD")])
    names = available_symbol_names(fake)
    assert names == ["EURUSD", "GBPUSD"]
    assert fake.call_count("order_send") == 0

