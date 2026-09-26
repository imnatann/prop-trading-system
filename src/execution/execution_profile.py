"""
FundingPips execution profile: measured broker characteristics, not assumptions.

Built from LIVE MT5 symbol/account metadata. Nothing here defaults to generic retail
FX values for spread, commission, swap, minimum lot, contract size, or pip size -
those are all captured from the terminal.

The profile is the bridge into research: it converts to
:class:src.execution.models.SimulationCostParameters, which is a plain data object
that simulator_v2 can consume WITHOUT importing MetaTrader5. The dependency arrow
therefore points execution -> research, never research -> execution.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.execution.models import (
    AccountInfo,
    SimulationCostParameters,
    SymbolInfo,
    TradeMode,
)
from src.execution.redaction import redact_mapping

from src.paths import EXECUTION_DIR as PROFILE_DIR
from src.paths import EXECUTION_PROFILE_PATH as PROFILE_PATH

#: Bump when the profile schema changes shape.
PROFILE_SCHEMA_VERSION = "1"


@dataclass
class SpreadStats:
    """Observed spread distribution for one symbol.

    Kept separate from the symbol spec because these are MEASURED, whereas the
    symbol spec is DECLARED by the broker.
    """

    samples: int = 0
    p50: Optional[float] = None
    p75: Optional[float] = None
    p90: Optional[float] = None
    p95: Optional[float] = None
    p99: Optional[float] = None
    mean: Optional[float] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    by_hour_utc: Dict[int, float] = field(default_factory=dict)
    by_session: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "samples": self.samples,
            "p50": self.p50,
            "p75": self.p75,
            "p90": self.p90,
            "p95": self.p95,
            "p99": self.p99,
            "mean": self.mean,
            "min": self.minimum,
            "max": self.maximum,
            "by_hour_utc": {str(k): v for k, v in sorted(self.by_hour_utc.items())},
            "by_session": dict(sorted(self.by_session.items())),
        }


@dataclass
class FillObservation:
    """One observed round trip. Populated only after real fills exist."""

    requested_price: float
    filled_price: float
    side: str
    volume: float
    slippage_price: float = 0.0
    slippage_pips: float = 0.0
    commission: float = 0.0
    swap: float = 0.0
    observed_at_utc: Optional[datetime] = None
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "requested_price": self.requested_price,
            "filled_price": self.filled_price,
            "side": self.side,
            "volume": self.volume,
            "slippage_price": self.slippage_price,
            "slippage_pips": self.slippage_pips,
            "commission": self.commission,
            "swap": self.swap,
            "observed_at_utc": self.observed_at_utc.isoformat() if self.observed_at_utc else None,
            "note": self.note,
        }


@dataclass
class FundingPipsExecutionProfile:
    """Everything the execution layer measured about FundingPips for one symbol."""

    canonical_symbol: str
    provider_symbol: str
    symbol: SymbolInfo
    account: Optional[AccountInfo] = None
    spread_stats: Optional[SpreadStats] = None
    fills: List[FillObservation] = field(default_factory=list)
    commission_per_lot_per_side: Optional[float] = None
    captured_at_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    schema_version: str = PROFILE_SCHEMA_VERSION
    notes: str = ""

    # ------------------------------------------------------------- derived
    @property
    def pip_size(self) -> float:
        return self.symbol.pip_size

    @property
    def swap_long_pips(self) -> float:
        """MT5 swap is quoted in POINTS per lot; convert with the derived pip size."""
        pip = self.pip_size
        if pip <= 0:
            return 0.0
        return (self.symbol.swap_long * self.symbol.point) / pip

    @property
    def swap_short_pips(self) -> float:
        pip = self.pip_size
        if pip <= 0:
            return 0.0
        return (self.symbol.swap_short * self.symbol.point) / pip

    @property
    def observed_slippage_pips(self) -> Optional[float]:
        """Mean absolute slippage across observed fills, in pips."""
        if not self.fills:
            return None
        pip = self.pip_size
        if pip <= 0:
            return None
        vals = [abs(f.slippage_pips) for f in self.fills if f.slippage_pips]
        if not vals:
            return None
        return sum(vals) / len(vals)

    # ---------------------------------------------------------- conversion
    def to_simulation_costs(
        self,
        spread_quantile: str = "p50",
        slippage_pips: Optional[float] = None,
        commission_per_lot_per_side: Optional[float] = None,
        fallback_spread_pips: Optional[float] = None,
    ) -> SimulationCostParameters:
        """Convert this measured profile into simulator_v2 cost inputs.

        Args:
            spread_quantile: which observed spread quantile to use. Defaults to p50
                (median). Callers doing cost-stress work can ask for p90/p95.
            slippage_pips: explicit override; otherwise the observed mean, otherwise
                the broker-declared spread as a conservative proxy is NOT used -
                we return 0.0 and record that no fill evidence exists.
            commission_per_lot_per_side: explicit override, else the measured value.
            fallback_spread_pips: used only when no spread telemetry exists yet.

        The returned object records how many samples backed it, so a consumer can
        refuse to trust a profile built on no observations.
        """
        spread: Optional[float] = None
        samples = 0
        stats = self.spread_stats
        if stats is not None and stats.samples > 0:
            samples = stats.samples
            spread = {
                "p50": stats.p50,
                "p75": stats.p75,
                "p90": stats.p90,
                "p95": stats.p95,
                "p99": stats.p99,
                "mean": stats.mean,
            }.get(spread_quantile)
            if spread is None:
                spread = stats.p50
            if spread is None:
                spread = stats.mean
        if spread is None:
            spread = fallback_spread_pips if fallback_spread_pips is not None else self.symbol.spread_pips

        slip = slippage_pips if slippage_pips is not None else self.observed_slippage_pips
        if slip is None:
            slip = 0.0

        commission = (
            commission_per_lot_per_side
            if commission_per_lot_per_side is not None
            else (self.commission_per_lot_per_side or 0.0)
        )

        notes = "profile_only"
        if samples == 0:
            notes = "no_spread_telemetry_declared_spread_used"
        if not self.fills:
            notes += ";no_fill_evidence_slippage_zero"

        return SimulationCostParameters(
            spread_pips=float(spread),
            slippage_pips=float(slip),
            commission_per_lot_per_side=float(commission),
            swap_long_pips=float(self.swap_long_pips),
            swap_short_pips=float(self.swap_short_pips),
            source="fundingpips_mt5",
            samples=samples,
            observed_at_utc=self.captured_at_utc,
            spread_p50=stats.p50 if stats else None,
            spread_p75=stats.p75 if stats else None,
            spread_p90=stats.p90 if stats else None,
            spread_p95=stats.p95 if stats else None,
            spread_p99=stats.p99 if stats else None,
            notes=notes,
        )

    # -------------------------------------------------------------- io
    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe view. Contains no credentials whatsoever."""
        data: Dict[str, Any] = {
            "schema_version": self.schema_version,
            "captured_at_utc": self.captured_at_utc.isoformat(),
            "canonical_symbol": self.canonical_symbol,
            "provider_symbol": self.provider_symbol,
            "symbol": self.symbol.to_dict(),
            "spread_stats": self.spread_stats.to_dict() if self.spread_stats else None,
            "fills": [f.to_dict() for f in self.fills],
            "commission_per_lot_per_side": self.commission_per_lot_per_side,
            "pip_size": self.pip_size,
            "swap_long_pips": self.swap_long_pips,
            "swap_short_pips": self.swap_short_pips,
            "observed_slippage_pips": self.observed_slippage_pips,
            "notes": self.notes,
        }
        if self.account is not None:
            data["account"] = self.account.to_safe_dict()
        return redact_mapping(data)

    def save(self, path: Optional[Path] = None) -> Path:
        """Atomically persist the profile. Never writes credentials."""
        target = Path(path) if path else PROFILE_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_dict(), indent=2, sort_keys=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(target)
        return target

    @classmethod
    def load(cls, path: Optional[Path] = None) -> Dict[str, Any]:
        """Read a saved profile back as a plain dict."""
        target = Path(path) if path else PROFILE_PATH
        return json.loads(target.read_text(encoding="utf-8"))


def build_profile(
    symbol_info: SymbolInfo,
    account: Optional[AccountInfo] = None,
    spread_stats: Optional[SpreadStats] = None,
    commission_per_lot_per_side: Optional[float] = None,
    notes: str = "",
) -> FundingPipsExecutionProfile:
    """Assemble a profile from captured MT5 metadata."""
    return FundingPipsExecutionProfile(
        canonical_symbol=symbol_info.canonical_symbol,
        provider_symbol=symbol_info.provider_symbol,
        symbol=symbol_info,
        account=account,
        spread_stats=spread_stats,
        commission_per_lot_per_side=commission_per_lot_per_side,
        notes=notes,
    )

