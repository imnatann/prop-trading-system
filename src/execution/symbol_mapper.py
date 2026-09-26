"""
Deterministic canonical -> provider symbol resolution.

We never assume the broker names the instrument exactly "EURUSD". FundingPips (like
most MT5 prop firms) may suffix symbols, e.g. EURUSD.r / EURUSD_raw / EURUSD.a.

Resolution order is explicit and deterministic:

    1. exact match                        EURUSD
    2. canonical dotted-suffix match      EURUSD.*
    3. canonical prefix match             EURUSD*
    4. known-variant suffix match         <variant>EURUSD / EURUSD<variant>

If more than one candidate matches at the SAME rank, resolution FAILS with
AmbiguousSymbolError. We never silently pick one: an ambiguous symbol is exactly how
an order lands on the wrong instrument.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from src.execution.errors import AmbiguousSymbolError, SymbolNotFoundError

#: Well-known broker suffixes/prefixes, checked after the generic patterns.
KNOWN_SUFFIXES: Sequence[str] = (
    "", ".r", ".raw", ".a", ".b", ".c", ".pro", ".ecn", ".m", ".micro",
    "_raw", "_ecn", "raw", "m", "pro",
)
KNOWN_PREFIXES: Sequence[str] = ("", "m", "micro.", "#")


@dataclass(frozen=True)
class SymbolResolution:
    """The outcome of a resolution: canonical name plus how we got there."""

    canonical_symbol: str
    provider_symbol: str
    match_rule: str
    candidates: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "canonical_symbol": self.canonical_symbol,
            "provider_symbol": self.provider_symbol,
            "match_rule": self.match_rule,
            "candidates": list(self.candidates),
        }


def _normalise(symbol: str) -> str:
    return (symbol or "").strip().upper()


def _strip_separators(symbol: str) -> str:
    """Collapse everything but A-Z0-9 so EUR/USD and EUR USD both become EURUSD."""
    return re.sub(r"[^A-Z0-9]", "", _normalise(symbol))


def resolve_symbol(
    canonical_symbol: str,
    available_symbols: Sequence[str],
    require_exact_first: bool = True,
) -> SymbolResolution:
    """Resolve a canonical symbol against the symbols the broker actually offers.

    Args:
        canonical_symbol: e.g. "EURUSD".
        available_symbols: the broker's symbol list, exactly as reported by MT5.
        require_exact_first: when True an exact match short-circuits (rank 1).

    Returns:
        SymbolResolution naming the provider symbol and the rule that matched.

    Raises:
        SymbolNotFoundError: no candidate at any rank.
        AmbiguousSymbolError: two or more candidates tie at the winning rank.
    """
    canonical = _normalise(canonical_symbol)
    if not canonical:
        raise SymbolNotFoundError("Canonical symbol is empty.")

    available = [s for s in (available_symbols or []) if s]
    if not available:
        raise SymbolNotFoundError(
            "The broker reported no available symbols for %r." % canonical
        )

    # Rank 1 - exact match. Unique by construction.
    if require_exact_first:
        for sym in available:
            if _normalise(sym) == canonical:
                return SymbolResolution(canonical, sym, "exact", [sym])

    # Rank 2 - dotted suffix: EURUSD.*
    dotted = [s for s in available if re.fullmatch(re.escape(canonical) + r"\.[A-Za-z0-9_]+", _normalise(s))]
    if dotted:
        return _settle(canonical, dotted, "canonical.dotted_suffix")

    # Rank 3 - prefix: EURUSD*
    prefixed = [s for s in available if _normalise(s).startswith(canonical) and _normalise(s) != canonical]
    if prefixed:
        exact_like = [s for s in prefixed if _strip_separators(s) == canonical]
        if exact_like:
            return _settle(canonical, exact_like, "canonical.separator_variant")
        return _settle(canonical, prefixed, "canonical.prefix")

    # Rank 4 - known variant affixes around the canonical name
    variants: List[str] = []
    for suffix in KNOWN_SUFFIXES:
        for prefix in KNOWN_PREFIXES:
            candidate = (prefix + canonical + suffix).upper()
            variants.extend(s for s in available if _normalise(s) == candidate)
    if variants:
        return _settle(canonical, variants, "known_variant")

    # Rank 5 - separator-insensitive full match (EUR/USD vs EURUSD)
    loose = [s for s in available if _strip_separators(s) == canonical]
    if loose:
        return _settle(canonical, loose, "separator_insensitive")

    raise SymbolNotFoundError(
        "No provider symbol could be resolved for canonical %r among %d available "
        "symbols. Refusing to guess." % (canonical, len(available)),
        canonical_symbol=canonical,
    )


def _settle(canonical: str, candidates: List[str], rule: str) -> SymbolResolution:
    """Accept a single winner, or fail loudly on a tie."""
    unique = sorted(set(candidates))
    if len(unique) > 1:
        raise AmbiguousSymbolError(
            "Ambiguous symbol resolution for %r under rule %r: %s. Multiple equally "
            "plausible provider symbols exist; refusing to select one silently."
            % (canonical, rule, ", ".join(unique)),
            canonical_symbol=canonical,
            candidates=unique,
        )
    return SymbolResolution(canonical, unique[0], rule, unique)


def available_symbol_names(mt5_module) -> List[str]:
    """Return every symbol name the terminal exposes.

    Reads only; never enables trading and never selects a symbol for trading.
    """
    symbols = mt5_module.symbols_get()
    if not symbols:
        return []
    names: List[str] = []
    for sym in symbols:
        name = getattr(sym, "name", None)
        if name:
            names.append(name)
    return names

