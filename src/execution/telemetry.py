"""
Execution telemetry: spread recording, structured events, cost attribution.

Telemetry lives HERE and nowhere else
------------------------------------
    data/execution/fundingpips/raw/        <- un-normalised provider records
    data/execution/fundingpips/canonical/  <- normalized UTC spread samples

FundingPips telemetry is EXECUTION CALIBRATION data. It is not, and must not become,
a historical alpha-validation dataset. Nothing writes research candles into these
directories, and nothing writes telemetry into a research directory.

Cost attribution is deliberately single-counted: net = gross - spread - slippage
- commission - financing, with slippage computed as the RESIDUAL of the actual fill
arithmetic. That is the only way to prove the spread was not charged twice.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from loguru import logger

from src.execution.execution_profile import SpreadStats
from src.execution.models import Tick
from src.execution.redaction import redact, redact_mapping

from src.paths import EXECUTION_CANONICAL_DIR as CANONICAL_DIR
from src.paths import EXECUTION_RAW_DIR as RAW_DIR

#: Pairs of (name, start_hour_utc, end_hour_utc) for session bucketing.
SESSIONS: Sequence[tuple] = (
    ("sydney", 21, 6),
    ("tokyo", 0, 9),
    ("london", 7, 16),
    ("newyork", 12, 21),
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(dt: datetime) -> datetime:
    """Coerce a datetime to timezone-aware UTC.

    MT5 terminal timestamps arrive as naive epoch seconds in the BROKER's timezone
    convention; callers convert with fromtimestamp(..., tz=timezone.utc) first.
    This helper only guarantees the result is aware and normalised.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# --------------------------------------------------------------------- events
def exec_event(
    component: str,
    event: str,
    success: Optional[bool] = None,
    reason: str = "",
    **fields: Any,
) -> Dict[str, Any]:
    """Emit one structured, redacted execution event and return the payload.

    Every field passes through redaction, so a caller cannot leak a credential by
    adding an unexpected key.
    """
    payload: Dict[str, Any] = {
        "timestamp": utc_now().isoformat(),
        "component": component,
        "event": event,
    }
    if success is not None:
        payload["success"] = bool(success)
    if reason:
        payload["reason"] = redact(reason)
    for key, value in fields.items():
        if value is None:
            continue
        payload[key] = redact(value) if isinstance(value, str) else value

    payload = redact_mapping(payload)
    level = "INFO"
    if success is False:
        level = "WARNING"
    if event.endswith("failure") or event.endswith("error"):
        level = "ERROR"
    logger.bind(component=component, event=event).log(level, json.dumps(payload, sort_keys=True))
    return payload


# -------------------------------------------------------------------- spread
@dataclass
class SpreadSample:
    """One observed quote, normalized to UTC."""

    timestamp_utc: datetime
    canonical_symbol: str
    provider_symbol: str
    bid: float
    ask: float
    point: float = 0.00001
    pip_size: float = 0.0001
    terminal_time_utc: Optional[datetime] = None
    local_receipt_utc: Optional[datetime] = None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_price(self) -> float:
        return self.ask - self.bid

    @property
    def spread_points(self) -> float:
        if self.point <= 0:
            return 0.0
        return self.spread_price / self.point

    @property
    def spread_pips(self) -> float:
        if self.pip_size <= 0:
            return 0.0
        return self.spread_price / self.pip_size

    @property
    def is_valid(self) -> bool:
        return self.bid > 0 and self.ask > 0 and self.ask > self.bid

    @property
    def latency_proxy_ms(self) -> Optional[float]:
        """Terminal->receipt latency, when both stamps are known."""
        if self.terminal_time_utc is None or self.local_receipt_utc is None:
            return None
        delta = (self.local_receipt_utc - self.terminal_time_utc).total_seconds() * 1000.0
        return delta

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp_utc": self.timestamp_utc.isoformat(),
            "terminal_time_utc": self.terminal_time_utc.isoformat() if self.terminal_time_utc else None,
            "local_receipt_utc": self.local_receipt_utc.isoformat() if self.local_receipt_utc else None,
            "latency_proxy_ms": self.latency_proxy_ms,
            "canonical_symbol": self.canonical_symbol,
            "provider_symbol": self.provider_symbol,
            "bid": self.bid,
            "ask": self.ask,
            "mid": self.mid,
            "spread_price": self.spread_price,
            "spread_points": self.spread_points,
            "spread_pips": self.spread_pips,
        }

    @classmethod
    def from_tick(cls, tick: Tick, pip_size: float, point: float) -> "SpreadSample":
        return cls(
            timestamp_utc=ensure_utc(tick.timestamp_utc),
            canonical_symbol=tick.canonical_symbol,
            provider_symbol=tick.provider_symbol,
            bid=tick.bid,
            ask=tick.ask,
            point=point,
            pip_size=pip_size,
            terminal_time_utc=ensure_utc(tick.terminal_time_utc) if tick.terminal_time_utc else None,
            local_receipt_utc=ensure_utc(tick.local_receipt_utc) if tick.local_receipt_utc else None,
        )


def _percentile(sorted_values: List[float], q: float) -> Optional[float]:
    """Linear-interpolation percentile. q in [0, 1]."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = q * (len(sorted_values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    frac = position - lower
    return sorted_values[lower] * (1.0 - frac) + sorted_values[upper] * frac


def session_for_hour(hour_utc: int) -> List[str]:
    """Return every session bucket containing this UTC hour (may overlap)."""
    names = []
    for name, start, end in SESSIONS:
        if start <= end:
            if start <= hour_utc < end:
                names.append(name)
        else:  # wraps midnight (Sydney)
            if hour_utc >= start or hour_utc < end:
                names.append(name)
    return names


class SpreadRecorder:
    """Accumulates spread samples and derives distribution statistics.

    Deliberately holds no broker reference and can submit no order.
    """

    def __init__(self, canonical_symbol: str, provider_symbol: str = "") -> None:
        self.canonical_symbol = canonical_symbol
        self.provider_symbol = provider_symbol
        self.samples: List[SpreadSample] = []
        self.rejected: int = 0

    def add(self, sample: SpreadSample) -> bool:
        """Record a sample. Crossed/invalid quotes are counted but not stored."""
        if not sample.is_valid:
            self.rejected += 1
            return False
        self.samples.append(sample)
        return True

    def add_tick(self, tick: Tick, pip_size: float, point: float) -> bool:
        return self.add(SpreadSample.from_tick(tick, pip_size, point))

    @property
    def count(self) -> int:
        return len(self.samples)

    def spread_series(self) -> List[float]:
        return [s.spread_pips for s in self.samples]

    def stats(self) -> SpreadStats:
        """Median/p75/p90/p95/p99, split by UTC hour and by session."""
        values = sorted(self.spread_series())
        if not values:
            return SpreadStats(samples=0)

        by_hour_raw: Dict[int, List[float]] = {}
        for sample in self.samples:
            by_hour_raw.setdefault(ensure_utc(sample.timestamp_utc).hour, []).append(sample.spread_pips)
        by_hour = {h: _percentile(sorted(v), 0.5) for h, v in by_hour_raw.items()}

        by_session: Dict[str, List[float]] = {}
        for sample in self.samples:
            for name in session_for_hour(ensure_utc(sample.timestamp_utc).hour):
                by_session.setdefault(name, []).append(sample.spread_pips)

        return SpreadStats(
            samples=len(values),
            p50=_percentile(values, 0.50),
            p75=_percentile(values, 0.75),
            p90=_percentile(values, 0.90),
            p95=_percentile(values, 0.95),
            p99=_percentile(values, 0.99),
            mean=sum(values) / len(values),
            minimum=values[0],
            maximum=values[-1],
            by_hour_utc={k: v for k, v in by_hour.items() if v is not None},
            by_session={k: _percentile(sorted(v), 0.5) for k, v in by_session.items()},
        )

    def write_jsonl(self, directory: Optional[Path] = None, filename: Optional[str] = None) -> Path:
        """Append samples as JSONL to the telemetry directory.

        Written with O_APPEND so a crash mid-run cannot rewrite earlier observations,
        and flushed+fsynced so Ctrl+C leaves a valid trailing line count.
        """
        target_dir = Path(directory) if directory else CANONICAL_DIR
        target_dir.mkdir(parents=True, exist_ok=True)
        name = filename or "spread_%s.jsonl" % ensure_utc(utc_now()).strftime("%Y%m%d")
        target = target_dir / name

        lines = "".join(json.dumps(s.to_dict(), sort_keys=True) + "\n" for s in self.samples)
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(lines)
            handle.flush()
            os.fsync(handle.fileno())
        return target

    def write_raw(self, ticks: Iterable[Dict[str, Any]], directory: Optional[Path] = None,
                  filename: Optional[str] = None) -> Path:
        """Persist un-normalized provider records for replay."""
        target_dir = Path(directory) if directory else RAW_DIR
        target_dir.mkdir(parents=True, exist_ok=True)
        name = filename or "ticks_%s.jsonl" % ensure_utc(utc_now()).strftime("%Y%m%d")
        target = target_dir / name
        payload = "".join(json.dumps(redact_mapping(t), sort_keys=True) + "\n" for t in ticks)
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        return target


# ----------------------------------------------------------- cost attribution
@dataclass
class CostBreakdown:
    """Single-counted decomposition of one round trip.

    Identity enforced at construction:
        net_pnl == gross_pnl - spread_cost - slippage_cost - commission - financing
    """

    symbol: str
    side: str
    volume: float
    entry_price: float
    exit_price: float
    entry_mid: float
    exit_mid: float
    gross_pnl: float
    spread_cost: float
    slippage_cost: float
    commission: float
    financing: float
    net_pnl: float
    pip_size: float = 0.0001
    contract_size: float = 100_000.0

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "symbol": self.symbol,
            "side": self.side,
            "volume": self.volume,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "entry_mid": self.entry_mid,
            "exit_mid": self.exit_mid,
            "gross_pnl": self.gross_pnl,
            "spread_cost": self.spread_cost,
            "slippage_cost": self.slippage_cost,
            "commission": self.commission,
            "financing": self.financing,
            "net_pnl": self.net_pnl,
            "gross_pips": self.gross_pips,
            "spread_pips": self.spread_pips,
            "slippage_pips": self.slippage_pips,
            "net_pips": self.net_pips,
            "round_trip_cost": self.round_trip_cost,
        }
        return data

    # --- pip-denominated views. Division is guarded so a zero pip never raises.
    def _to_pips(self, cash: float) -> float:
        if self.pip_size <= 0 or self.volume <= 0 or self.contract_size <= 0:
            return 0.0
        return cash / (self.pip_size * self.volume * self.contract_size)

    @property
    def gross_pips(self) -> float:
        return self._to_pips(self.gross_pnl)

    @property
    def spread_pips(self) -> float:
        return self._to_pips(self.spread_cost)

    @property
    def slippage_pips(self) -> float:
        return self._to_pips(self.slippage_cost)

    @property
    def net_pips(self) -> float:
        return self._to_pips(self.net_pnl)

    @property
    def round_trip_cost(self) -> float:
        """Total cost of the round trip, cash terms. Spread counted EXACTLY once."""
        return self.spread_cost + self.slippage_cost + self.commission + self.financing


def attribute_round_trip(
    symbol: str,
    side: str,
    volume: float,
    entry_price: float,
    exit_price: float,
    entry_mid: float,
    exit_mid: float,
    entry_spread_price: float = 0.0,
    exit_spread_price: float = 0.0,
    pip_size: float = 0.0001,
    contract_size: float = 100_000.0,
    commission: float = 0.0,
    financing: float = 0.0,
    tolerance: float = 1e-9,
) -> CostBreakdown:
    """Decompose a round trip into gross / spread / slippage / commission / financing.

    Why the observed spread must be supplied
    ----------------------------------------
    A fill price alone cannot separate spread from slippage: a fill 1.2 pips from
    mid could be a 1.2-pip spread with no slippage, or a 0.6-pip spread with 0.6
    pips of slippage. Inferring the spread from |fill - mid| would force slippage to
    be identically zero - which is exactly the bug this signature exists to avoid.
    The caller therefore passes the OBSERVED bid/ask spread at entry and at exit,
    taken from the quote stream.

    Method
    ------
    The cash truth is the actual fill arithmetic:
        realized = (exit_price - entry_price) * direction * notional
    The pure-alpha component is the mid-to-mid move:
        gross    = (exit_mid - entry_mid) * direction * notional
    The difference is total execution cost, split by:
        spread_cost   = (half_spread_entry + half_spread_exit) * notional
        slippage_cost = per-side adverse deviation from the expected touch price

    Expected touch price, i.e. where a clean fill should have landed:
        BUY  entry at ask = entry_mid + half_spread
        BUY  exit  at bid = exit_mid  - half_spread
        SELL entry at bid = entry_mid - half_spread
        SELL exit  at ask = exit_mid  + half_spread

    Slippage is signed and ADVERSE-POSITIVE: positive is a cost, negative is a
    favourable fill. Because spread and slippage are derived from independent
    inputs, the identity
        spread_cost + slippage_cost == realized execution cost
    is verified at the end rather than assumed.
    """
    direction = 1.0 if str(side).upper() == "BUY" else -1.0
    notional = volume * contract_size
    half_entry = abs(entry_spread_price) / 2.0
    half_exit = abs(exit_spread_price) / 2.0

    gross = (exit_mid - entry_mid) * direction * notional
    realized = (exit_price - entry_price) * direction * notional
    exec_cost_total = gross - realized

    if direction > 0:  # BUY buys the ask and sells the bid
        slip_entry = entry_price - (entry_mid + half_entry)
        slip_exit = (exit_mid - half_exit) - exit_price
    else:              # SELL sells the bid and buys the ask
        slip_entry = (entry_mid - half_entry) - entry_price
        slip_exit = exit_price - (exit_mid + half_exit)

    spread_cost = (half_entry + half_exit) * notional
    slippage_cost = (slip_entry + slip_exit) * notional
    net = gross - spread_cost - slippage_cost - commission - financing

    breakdown = CostBreakdown(
        symbol=symbol,
        side=str(side).upper(),
        volume=volume,
        entry_price=entry_price,
        exit_price=exit_price,
        entry_mid=entry_mid,
        exit_mid=exit_mid,
        gross_pnl=gross,
        spread_cost=spread_cost,
        slippage_cost=slippage_cost,
        commission=commission,
        financing=financing,
        net_pnl=net,
        pip_size=pip_size,
        contract_size=contract_size,
    )

    # THE invariant: the two cost components must reconstruct the true execution
    # cost exactly. If they do not, the split is wrong and we refuse to report it.
    reconstructed = spread_cost + slippage_cost
    if abs(reconstructed - exec_cost_total) > tolerance:
        raise AssertionError(
            "cost attribution does not reconstruct execution cost for %s: "
            "spread+slippage=%r but execution cost=%r"
            % (symbol, reconstructed, exec_cost_total)
        )
    return breakdown
