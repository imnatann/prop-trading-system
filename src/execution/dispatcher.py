"""
Order Dispatcher & Safe Execution Engine.
Menghubungkan Pre-Trade Risk Gatekeeper, OMS SQLite WAL, dan Broker Adapter
dengan proteksi idempotensi dan penanganan timeout/unknown state.
"""

from datetime import datetime, timezone
from typing import List, Optional
from loguru import logger

from src.broker.base import BaseBrokerAdapter, OrderResult
from src.broker.models import SymbolSpec
from src.data.news_calendar import EconomicEvent
from src.execution.idempotency import IdempotencyRegistry, generate_client_order_id
from src.oms.models import Order
from src.oms.service import OMSService
from src.oms.states import OrderState
from src.risk.gatekeeper import RiskGatekeeper
from src.strategy.base import TradeSignal, SignalAction


class OrderDispatcher:
    """
    Dispatcher eksekusi order prop trading yang aman dan tahan banting.
    Menjamin: "Persist order intent into SQLite BEFORE external side effect".
    """

    def __init__(
        self,
        oms: OMSService,
        broker: BaseBrokerAdapter,
        gatekeeper: Optional[RiskGatekeeper] = None,
        idempotency: Optional[IdempotencyRegistry] = None,
        prop_prefix: str = "FP"
    ):
        self.oms = oms
        self.broker = broker
        self.gatekeeper = gatekeeper
        self.idempotency = idempotency or IdempotencyRegistry()
        self.prop_prefix = prop_prefix

    def dispatch(
        self,
        signal: TradeSignal,
        lot_size: Optional[float] = None,
        client_order_id: Optional[str] = None,
        economic_events: Optional[List[EconomicEvent]] = None
    ) -> OrderResult:
        """
        Alur eksekusi aman:
        1. Validasi idempotensi ID
        2. Inisiasi & persist order di OMS (State: CREATED)
        3. Validasi Pre-Trade Risk Gatekeeper (jika diaktifkan)
        4. Persist state SUBMITTING ke SQLite WAL SEBELUM memanggil broker
        5. Eksekusi ke broker adapter
        6. Update state (ACK/FILLED) atau tangani UNKNOWN saat timeout
        """
        # 1. Pastikan Client Order ID unik & terdaftar
        cid = client_order_id or generate_client_order_id(self.prop_prefix, signal.symbol)
        if not self.idempotency.acquire(cid):
            reason = f"Duplicate or concurrent order rejected for Client Order ID {cid}"
            logger.warning(f"Dispatcher: {reason}")
            return OrderResult(
                success=False,
                order_id="",
                client_order_id=cid,
                symbol=signal.symbol,
                action=signal.action.value,
                lot_size=lot_size or 0.0,
                price=0.0,
                sl=signal.stop_loss,
                tp=signal.take_profit,
                status="REJECTED",
                error_message=reason
            )

        # 2. Persist intent awal ke OMS (Fail-Closed if DB fails)
        initial_lot = lot_size if lot_size is not None else 0.0
        try:
            order = self.oms.create_order(
                client_order_id=cid,
                symbol=signal.symbol,
                action=signal.action.value,
                requested_lot=initial_lot,
                sl_price=signal.stop_loss,
                tp_price=signal.take_profit
            )
        except Exception as e:
            err_msg = f"Database failure (Fail-Closed NO NEW RISK): {str(e)}"
            logger.critical(f"Dispatcher: {err_msg}")
            self.idempotency.release_failure(cid)
            return OrderResult(
                success=False,
                order_id="",
                client_order_id=cid,
                symbol=signal.symbol,
                action=signal.action.value,
                lot_size=0.0,
                price=0.0,
                sl=signal.stop_loss,
                tp=signal.take_profit,
                status="REJECTED",
                error_message=err_msg
            )

        # 3. Validasi Pre-Trade Risk Gatekeeper jika tersedia
        final_lot = initial_lot
        if self.gatekeeper:
            account = self.broker.get_account_snapshot()
            open_count = self.broker.get_open_positions_count()
            _, _, spread_pips = self.broker.get_symbol_price(signal.symbol)
            spec = self.broker.get_symbol_spec(signal.symbol)

            decision = self.gatekeeper.evaluate(
                signal=signal,
                account=account,
                open_positions_count=open_count,
                current_spread_pips=spread_pips,
                economic_events=economic_events or [],
                spec=spec
            )

            if not decision.is_approved:
                self.oms.mark_rejected(order.order_id, reason=decision.rejection_reason or "Risk gatekeeper reject")
                self.idempotency.release_failure(cid)
                return OrderResult(
                    success=False,
                    order_id=order.order_id,
                    client_order_id=cid,
                    symbol=signal.symbol,
                    action=signal.action.value,
                    lot_size=0.0,
                    price=0.0,
                    sl=signal.stop_loss,
                    tp=signal.take_profit,
                    status="REJECTED",
                    error_message=decision.rejection_reason or "Risk gatekeeper reject"
                )

            final_lot = decision.lot_size
            order.requested_lot = final_lot
            self.oms.repo.save_order(order)
            self.oms.mark_risk_approved(order.order_id, payload={"approved_lot": final_lot})

        # 4. Tandai SUBMITTING di SQLite WAL TEPAT SEBELUM network I/O
        self.oms.mark_submitting(order.order_id)

        # 5. Panggil Broker Adapter
        try:
            result = self.broker.execute_order(
                symbol=signal.symbol,
                action=signal.action,
                lot_size=final_lot,
                sl=signal.stop_loss,
                tp=signal.take_profit,
                client_order_id=cid
            )
        except Exception as exc:
            # NETWORK TIMEOUT / CRASH: Pindah ke state UNKNOWN dan coba recovery
            err_msg = f"Network or broker execution error: {str(exc)}"
            logger.critical(f"Dispatcher: Order {cid} encounter exception -> marking UNKNOWN. Err: {err_msg}")
            self.oms.mark_unknown(order.order_id, reason=err_msg)

            # Upayakan immediate query recovery
            recovery = self._attempt_broker_recovery(cid, order)
            if recovery:
                self.idempotency.release_success(cid)
                return recovery

            self.idempotency.release_failure(cid)
            return OrderResult(
                success=False,
                order_id=order.order_id,
                client_order_id=cid,
                symbol=signal.symbol,
                action=signal.action.value,
                lot_size=final_lot,
                price=0.0,
                sl=signal.stop_loss,
                tp=signal.take_profit,
                status="UNKNOWN",
                error_message=err_msg
            )

        # 6. Evaluasi Respon Normal Broker
        if result.success:
            self.oms.mark_ack(order.order_id, broker_order_id=result.order_id)
            self.oms.mark_filled(
                order_id=order.order_id,
                fill_price=result.price,
                filled_lot=result.lot_size,
                broker_order_id=result.order_id
            )
            self.idempotency.release_success(cid)
            return result
        else:
            self.oms.mark_rejected(order.order_id, reason=result.error_message or "Broker rejection")
            self.idempotency.release_failure(cid)
            return result

    def _attempt_broker_recovery(self, client_order_id: str, order: Order) -> Optional[OrderResult]:
        """Query balik ke broker menggunakan client_order_id untuk mendeteksi apakah order masuk."""
        try:
            broker_order = self.broker.get_order(client_order_id=client_order_id)
            if broker_order and broker_order.success:
                logger.info(f"Dispatcher: Successfully recovered order {client_order_id} from broker query!")
                self.oms.mark_ack(order.order_id, broker_order_id=broker_order.order_id)
                self.oms.mark_filled(
                    order_id=order.order_id,
                    fill_price=broker_order.price,
                    filled_lot=broker_order.lot_size,
                    broker_order_id=broker_order.order_id
                )
                return broker_order
        except Exception as e:
            logger.error(f"Dispatcher: Recovery query failed for {client_order_id}: {e}")
        return None
