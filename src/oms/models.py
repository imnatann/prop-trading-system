"""
OMS Domain Models & Records.
Representasi data struktural untuk Order, Event, Fill, dan Posisi di penyimpanan SQLite.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from src.oms.states import OrderState, OrderEventType


@dataclass
class Order:
    order_id: str                      # Internal unique ID (UUID)
    client_order_id: str               # Idempotent Client Order ID (e.g. QP-FP-20260920-...)
    symbol: str
    action: str                        # BUY or SELL
    requested_lot: float
    filled_lot: float = 0.0
    limit_price: Optional[float] = None
    sl_price: float = 0.0
    tp_price: float = 0.0
    broker_order_id: Optional[str] = None
    state: OrderState = OrderState.CREATED
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class OrderEvent:
    event_id: str
    order_id: str
    client_order_id: str
    event_type: OrderEventType
    from_state: OrderState
    to_state: OrderState
    timestamp_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Fill:
    fill_id: str
    order_id: str
    symbol: str
    volume: float
    price: float
    commission: float = 0.0
    swap: float = 0.0
    timestamp_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class PositionRecord:
    position_id: str
    client_order_id: Optional[str]
    symbol: str
    action: str
    volume: float
    entry_price: float
    sl: float
    tp: float
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    status: str = "OPEN"               # OPEN, CLOSED
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    closed_at: Optional[datetime] = None
