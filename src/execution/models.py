"""
Canonical execution-domain models for the FundingPips MT5 layer.

These are broker-agnostic value objects. Alpha/strategy code exchanges OrderIntent
objects with the execution layer; nothing here imports MetaTrader5 or any concrete
alpha implementation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from src.execution.redaction import mask_account, redact


class TradeMode(str, Enum):
    """Maps MT5 SYMBOL_TRADE_MODE_* constants into a stable domain enum."""

    DISABLED = "DISABLED"
    LONG_ONLY = "LONG_ONLY"
    SHORT_ONLY = "SHORT_ONLY"
    CLOSE_ONLY = "CLOSE_ONLY"
    FULL = "FULL"
    UNKNOWN = "UNKNOWN"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    SUBMITTED = "SUBMITTED"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"
    DISABLED = "DISABLED"


@dataclass(frozen=True)
class AccountInfo:
    """Account snapshot. Deliberately carries NO credentials.

    login is retained because the adapter must verify identity, but it is masked in
    every rendered form so that a print/log accident cannot publish it.
    """

    login: int
    server: str
    currency: str = ""
    balance: float = 0.0
    equity: float = 0.0
    margin: float = 0.0
    margin_free: float = 0.0
    margin_level: float = 0.0
    leverage: int = 0
    trade_allowed: bool = False
    trade_expert: bool = False
    connected: bool = False
    company: str = ""
    is_demo: Optional[bool] = None

    @property
    def masked_login(self) -> str:
        return mask_account(self.login)

    def to_safe_dict(self) -> Dict[str, Any]:
        """JSON-safe view with the account number masked."""
        return {
            "login_masked": self.masked_login,
            "server": self.server,
            "currency": self.currency,
            "balance": self.balance,
            "equity": self.equity,
            "margin": self.margin,
            "margin_free": self.margin_free,
            "margin_level": self.margin_level,
            "leverage": self.leverage,
            "trade_allowed": self.trade_allowed,
            "trade_expert": self.trade_expert,
            "connected": self.connected,
            "company": self.company,
            "is_demo": self.is_demo,
        }

    def __repr__(self) -> str:
        return (
            "AccountInfo(login=%s, server=%r, balance=%s, equity=%s, "
            "trade_allowed=%s, connected=%s)"
            % (
                self.masked_login,
                redact(self.server),
                self.balance,
                self.equity,
                self.trade_allowed,
                self.connected,
            )
        )


@dataclass(frozen=True)
class SymbolInfo:
    """Broker-native symbol specification, captured - never assumed."""

    canonical_symbol: str
    provider_symbol: str
    digits: int
    point: float
    trade_tick_size: float
    trade_tick_value: float
    contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level: int = 0
    freeze_level: int = 0
    trade_mode: TradeMode = TradeMode.UNKNOWN
    swap_long: float = 0.0
    swap_short: float = 0.0
    swap_mode: int = 0
    swap_rollover3days: int = 0
    currency_base: str = ""
    currency_profit: str = ""
    currency_margin: str = ""
    execution_mode: Optional[int] = None
    filling_modes: Optional[int] = None
    spread: int = 0
    visible: bool = True
    select_ok: bool = True

    @property
    def pip_size(self) -> float:
        """Pip size derived from symbol metadata, not hardcoded.

        Convention: a 5-digit FX quote (and its 3-digit JPY cousin) has a pip of
        10 points. Symbols that are not 3/5-digit (metals, indices, crypto) are
        treated as pip == point rather than being forced into the FX convention.
        """
        if self.point <= 0:
            return self.trade_tick_size or 0.0
        if self.digits in (3, 5):
            return self.point * 10.0
        return self.point

    @property
    def spread_pips(self) -> float:
        """Broker-reported spread (in points) expressed in pips."""
        pip = self.pip_size
        if pip <= 0:
            return 0.0
        return (self.spread * self.point) / pip

    @property
    def is_tradable(self) -> bool:
        return self.visible and self.trade_mode not in (TradeMode.DISABLED, TradeMode.UNKNOWN)

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe representation for the execution_profile.json artifact."""
        return {
            "canonical_symbol": self.canonical_symbol,
            "provider_symbol": self.provider_symbol,
            "digits": self.digits,
            "point": self.point,
            "trade_tick_size": self.trade_tick_size,
            "trade_tick_value": self.trade_tick_value,
            "contract_size": self.contract_size,
            "volume_min": self.volume_min,
            "volume_max": self.volume_max,
            "volume_step": self.volume_step,
            "stops_level": self.stops_level,
            "freeze_level": self.freeze_level,
            "trade_mode": self.trade_mode.value,
            "swap_long": self.swap_long,
            "swap_short": self.swap_short,
            "swap_mode": self.swap_mode,
            "swap_rollover3days": self.swap_rollover3days,
            "currency_base": self.currency_base,
            "currency_profit": self.currency_profit,
            "currency_margin": self.currency_margin,
            "execution_mode": self.execution_mode,
            "filling_modes": self.filling_modes,
            "spread_points": self.spread,
            "pip_size": self.pip_size,
            "spread_pips": self.spread_pips,
            "visible": self.visible,
            "is_tradable": self.is_tradable,
        }


@dataclass(frozen=True)
class Tick:
    """A single normalized quote. Always UTC."""

    canonical_symbol: str
    provider_symbol: str
    bid: float
    ask: float
    timestamp_utc: datetime
    terminal_time_utc: Optional[datetime] = None
    local_receipt_utc: Optional[datetime] = None
    last: float = 0.0
    volume: float = 0.0
    flags: int = 0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_price(self) -> float:
        return self.ask - self.bid

    @property
    def is_valid(self) -> bool:
        """bid < ask and both positive. A crossed or zero quote is not tradable."""
        return self.bid > 0 and self.ask > 0 and self.ask > self.bid

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp_utc": self.timestamp_utc.isoformat(),
            "terminal_time_utc": self.terminal_time_utc.isoformat() if self.terminal_time_utc else None,
            "local_receipt_utc": self.local_receipt_utc.isoformat() if self.local_receipt_utc else None,
            "canonical_symbol": self.canonical_symbol,
            "provider_symbol": self.provider_symbol,
            "bid": self.bid,
            "ask": self.ask,
            "mid": self.mid,
            "spread_price": self.spread_price,
        }


@dataclass
class OrderIntent:
    """Broker-agnostic order request produced by risk sizing.

    This is the ONLY object strategy/risk code hands to an ExecutionBroker.
    """

    symbol: str
    side: OrderSide
    volume: float
    sl: float = 0.0
    tp: float = 0.0
    client_order_id: Optional[str] = None
    comment: str = "QP"
    deviation: int = 10
    magic: int = 888999
    max_spread_pips: Optional[float] = None
    strategy_id: Optional[str] = None


@dataclass(frozen=True)
class OrderResultInfo:
    """Normalized broker response."""

    success: bool
    order_id: Optional[str]
    client_order_id: Optional[str]
    symbol: str
    side: str
    volume: float
    requested_price: float
    filled_price: float
    sl: float = 0.0
    tp: float = 0.0
    status: OrderStatus = OrderStatus.UNKNOWN
    retcode: Optional[int] = None
    retcode_name: str = ""
    error_message: Optional[str] = None
    comment: str = ""
    timestamp_utc: Optional[datetime] = None
    commission: float = 0.0
    swap: float = 0.0

    @property
    def slippage_price(self) -> float:
        """Signed price difference between request and fill.

        Positive means the fill was WORSE than requested:
          BUY  : filled higher than requested  -> filled - requested > 0
          SELL : filled lower  than requested  -> requested - filled > 0
        """
        if not self.filled_price or not self.requested_price:
            return 0.0
        if str(self.side).upper() == "BUY":
            return self.filled_price - self.requested_price
        return self.requested_price - self.filled_price

    def slippage_pips(self, pip_size: float) -> float:
        if pip_size <= 0:
            return 0.0
        return self.slippage_price / pip_size

    def __repr__(self) -> str:
        return (
            "OrderResultInfo(success=%s, order_id=%r, symbol=%r, side=%r, "
            "volume=%s, filled_price=%s, status=%s)"
            % (
                self.success,
                self.order_id,
                self.symbol,
                self.side,
                self.volume,
                self.filled_price,
                self.status.value,
            )
        )


@dataclass(frozen=True)
class PositionSnapshot:
    """Read-only view of an open position."""

    position_id: str
    symbol: str
    side: str
    volume: float
    entry_price: float
    current_price: float
    sl: float = 0.0
    tp: float = 0.0
    profit: float = 0.0
    swap: float = 0.0
    commission: float = 0.0
    magic: int = 0
    comment: str = ""
    open_time_utc: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "position_id": self.position_id,
            "symbol": self.symbol,
            "side": self.side,
            "volume": self.volume,
            "entry_price": self.entry_price,
            "current_price": self.current_price,
            "sl": self.sl,
            "tp": self.tp,
            "profit": self.profit,
            "swap": self.swap,
            "commission": self.commission,
            "magic": self.magic,
            "comment": self.comment,
            "open_time_utc": self.open_time_utc.isoformat() if self.open_time_utc else None,
        }


@dataclass(frozen=True)
class PendingOrderSnapshot:
    position_id: str
    symbol: str
    side: str
    volume: float
    price_open: float
    sl: float = 0.0
    tp: float = 0.0
    magic: int = 0
    comment: str = ""
    time_utc: Optional[datetime] = None


@dataclass
class SimulationCostParameters:
    """Calibrated cost inputs destined for simulator_v2's CostSpec.

    Deliberately a plain data object with NO MT5 dependency: the calibration layer
    converts a FundingPipsExecutionProfile into this, and simulator_v2 consumes it.
    simulator_v2 stays deterministic and broker-independent.
    """

    spread_pips: float
    slippage_pips: float
    commission_per_lot_per_side: float
    swap_long_pips: float
    swap_short_pips: float
    source: str = "fundingpips_mt5"
    samples: int = 0
    observed_at_utc: Optional[datetime] = None
    spread_p50: Optional[float] = None
    spread_p75: Optional[float] = None
    spread_p90: Optional[float] = None
    spread_p95: Optional[float] = None
    spread_p99: Optional[float] = None
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "spread_pips": self.spread_pips,
            "slippage_pips": self.slippage_pips,
            "commission_per_lot_per_side": self.commission_per_lot_per_side,
            "swap_long_pips": self.swap_long_pips,
            "swap_short_pips": self.swap_short_pips,
            "source": self.source,
            "samples": self.samples,
            "observed_at_utc": self.observed_at_utc.isoformat() if self.observed_at_utc else None,
            "spread_p50": self.spread_p50,
            "spread_p75": self.spread_p75,
            "spread_p90": self.spread_p90,
            "spread_p95": self.spread_p95,
            "spread_p99": self.spread_p99,
            "notes": self.notes,
        }

