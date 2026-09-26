"""
OMS Service & Order Lifecycle Coordinator.
Menerapkan State Machine formal dan menjamin kepatuhan:
"Persist order intent into SQLite BEFORE external side effect".
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from loguru import logger

from src.oms.models import Order, OrderEvent, PositionRecord
from src.oms.states import OrderState, OrderEventType
from src.oms.repository import OMSRepository


class OMSService:
    """Manajer siklus hidup transaksi dan transisi state order."""

    def __init__(self, repository: OMSRepository):
        self.repo = repository

    def create_order(
        self,
        client_order_id: str,
        symbol: str,
        action: str,
        requested_lot: float,
        sl_price: float,
        tp_price: float,
        limit_price: Optional[float] = None
    ) -> Order:
        """Membuat order baru dalam state CREATED dan langsung dipersist ke SQLite."""
        order_id = str(uuid.uuid4())
        order = Order(
            order_id=order_id,
            client_order_id=client_order_id,
            symbol=symbol.upper(),
            action=action.upper(),
            requested_lot=requested_lot,
            limit_price=limit_price,
            sl_price=sl_price,
            tp_price=tp_price,
            state=OrderState.CREATED
        )
        self.repo.save_order(order)
        self._record_transition(order, OrderState.CREATED, OrderEventType.ORDER_CREATED, {})
        logger.info(f"OMS: Order created #{order.client_order_id} ({order.symbol} {order.action} {order.requested_lot} lot)")
        return order

    def _record_transition(
        self,
        order: Order,
        to_state: OrderState,
        event_type: OrderEventType,
        payload: Dict[str, Any]
    ) -> None:
        from_state = order.state
        order.state = to_state
        order.updated_at = datetime.now(timezone.utc)
        self.repo.save_order(order)

        event = OrderEvent(
            event_id=str(uuid.uuid4()),
            order_id=order.order_id,
            client_order_id=order.client_order_id,
            event_type=event_type,
            from_state=from_state,
            to_state=to_state,
            payload=payload
        )
        self.repo.record_event(event)

    def mark_risk_approved(self, order_id: str, payload: Optional[Dict[str, Any]] = None) -> Order:
        order = self.repo.get_order(order_id)
        if not order:
            raise ValueError(f"Order {order_id} not found")
        self._record_transition(order, OrderState.RISK_APPROVED, OrderEventType.RISK_APPROVED, payload or {})
        return order

    def mark_submitting(self, order_id: str) -> Order:
        """Dipanggil TEPAT SEBELUM request jaringan dikirim ke broker."""
        order = self.repo.get_order(order_id)
        if not order:
            raise ValueError(f"Order {order_id} not found")
        self._record_transition(order, OrderState.SUBMITTING, OrderEventType.BROKER_SUBMIT, {})
        return order

    def mark_sent(self, order_id: str) -> Order:
        order = self.repo.get_order(order_id)
        if not order:
            raise ValueError(f"Order {order_id} not found")
        self._record_transition(order, OrderState.SENT, OrderEventType.BROKER_SUBMIT, {})
        return order

    def mark_ack(self, order_id: str, broker_order_id: str) -> Order:
        order = self.repo.get_order(order_id)
        if not order:
            raise ValueError(f"Order {order_id} not found")
        order.broker_order_id = broker_order_id
        self._record_transition(order, OrderState.ACKNOWLEDGED, OrderEventType.BROKER_ACK, {"broker_order_id": broker_order_id})
        return order

    def mark_filled(
        self,
        order_id: str,
        fill_price: float,
        filled_lot: float,
        broker_order_id: Optional[str] = None
    ) -> Order:
        order = self.repo.get_order(order_id)
        if not order:
            raise ValueError(f"Order {order_id} not found")
        order.filled_lot = filled_lot
        if broker_order_id:
            order.broker_order_id = broker_order_id
        self._record_transition(
            order,
            OrderState.FILLED,
            OrderEventType.FILL,
            {"fill_price": fill_price, "filled_lot": filled_lot, "broker_order_id": broker_order_id}
        )

        # Buat PositionRecord internal di DB
        pos_id = order.broker_order_id or order.client_order_id
        pos = PositionRecord(
            position_id=pos_id,
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            action=order.action,
            volume=filled_lot,
            entry_price=fill_price,
            sl=order.sl_price,
            tp=order.tp_price,
            current_price=fill_price,
            status="OPEN"
        )
        self.repo.upsert_position(pos)
        logger.info(f"OMS: Order #{order.client_order_id} FILLED @ {fill_price}. Position #{pos_id} opened.")
        return order

    def mark_rejected(self, order_id: str, reason: str) -> Order:
        order = self.repo.get_order(order_id)
        if not order:
            raise ValueError(f"Order {order_id} not found")
        self._record_transition(order, OrderState.REJECTED, OrderEventType.REJECTED, {"reason": reason})
        logger.warning(f"OMS: Order #{order.client_order_id} REJECTED: {reason}")
        return order

    def mark_unknown(self, order_id: str, reason: str) -> Order:
        """Dipanggil jika koneksi ke broker timeout/terputus saat pengiriman."""
        order = self.repo.get_order(order_id)
        if not order:
            raise ValueError(f"Order {order_id} not found")
        self._record_transition(order, OrderState.UNKNOWN, OrderEventType.TIMEOUT, {"reason": reason})
        logger.critical(f"OMS: Order #{order.client_order_id} marked UNKNOWN: {reason}. Awaiting reconciliation!")
        return order

    def mark_cancelled(self, order_id: str, reason: str) -> Order:
        order = self.repo.get_order(order_id)
        if not order:
            raise ValueError(f"Order {order_id} not found")
        self._record_transition(order, OrderState.CANCELLED, OrderEventType.CANCELLED, {"reason": reason})
        return order
