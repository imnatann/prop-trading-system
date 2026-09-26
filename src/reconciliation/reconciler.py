"""
Reconciliation Engine & Discrepancy Resolver.
Menjaga konsistensi antara authoritative SQLite WAL OMS dan Broker State.
Menjalankan fail-closed principle: jika ada anomali berisiko, kunci risk (LOCK NEW RISK).
"""

from datetime import datetime, timezone
from typing import List, Optional
from loguru import logger

from src.broker.base import BaseBrokerAdapter, PositionInfo
from src.oms.models import PositionRecord
from src.oms.repository import OMSRepository
from src.oms.service import OMSService
from src.oms.states import OrderState
from src.risk.drawdown_monitor import DrawdownMonitor
from src.reconciliation.models import (
    DiscrepancyType,
    DiscrepancyAction,
    DiscrepancyReport,
    ReconciliationSummary,
)
from src.reconciliation.policies import ReconciliationPolicy


class ReconciliationEngine:
    """Mesin rekonsiliasi state trading dan pelindung integritas modal."""

    def __init__(
        self,
        oms: OMSService,
        broker: BaseBrokerAdapter,
        drawdown_monitor: Optional[DrawdownMonitor] = None,
        policy: Optional[ReconciliationPolicy] = None
    ):
        self.oms = oms
        self.repo: OMSRepository = oms.repo
        self.broker = broker
        self.drawdown_monitor = drawdown_monitor
        self.policy = policy or ReconciliationPolicy()
        self.is_risk_locked = False

    def reconcile(self) -> ReconciliationSummary:
        """
        Melakukan rekonsiliasi komprehensif 360-derajat:
        1. Integritas koneksi broker
        2. Kepatuhan batas darurat akun (Emergency Kill Switch)
        3. Order in-flight/unknown yang tertinggal
        4. Sinkronisasi posisi (Ghost position & Orphan position)
        """
        reports: List[DiscrepancyReport] = []
        now = datetime.now(timezone.utc)

        # 1. Health & Connection Check
        health = self.broker.health()
        if not health.get("connected", True):
            self.is_risk_locked = True
            reports.append(DiscrepancyReport(
                discrepancy_type=DiscrepancyType.EMERGENCY_LIMIT_BREACH,
                action_taken=DiscrepancyAction.LOCK_NEW_RISK,
                details={"error": "Broker connection is down"}
            ))
            return ReconciliationSummary(is_clean=False, risk_locked=True, discrepancies=reports)

        # 2. Emergency Drawdown Verification
        if self.drawdown_monitor:
            account = self.broker.get_account_snapshot()
            daily_loss = self.drawdown_monitor.calculate_daily_loss_pct(account.equity)
            total_loss = self.drawdown_monitor.calculate_total_loss_pct(account.equity)

            if daily_loss >= self.drawdown_monitor.max_daily_loss_pct or total_loss >= self.drawdown_monitor.max_total_loss_pct:
                logger.critical(
                    f"RECONCILER EMERGENCY: Breach detected (Daily: {daily_loss:.2f}%, Total: {total_loss:.2f}%). "
                    "Initiating EMERGENCY_HALT_AND_LIQUIDATE."
                )
                self.is_risk_locked = True
                if self.policy.emergency_liquidate_on_breach:
                    closed = self.broker.close_all_positions()
                    reports.append(DiscrepancyReport(
                        discrepancy_type=DiscrepancyType.EMERGENCY_LIMIT_BREACH,
                        action_taken=DiscrepancyAction.EMERGENCY_HALT_AND_LIQUIDATE,
                        details={"closed_positions": closed, "daily_loss": daily_loss, "total_loss": total_loss}
                    ))
                else:
                    reports.append(DiscrepancyReport(
                        discrepancy_type=DiscrepancyType.EMERGENCY_LIMIT_BREACH,
                        action_taken=DiscrepancyAction.LOCK_NEW_RISK,
                        details={"daily_loss": daily_loss, "total_loss": total_loss}
                    ))
                return ReconciliationSummary(is_clean=False, risk_locked=True, discrepancies=reports)

        # 3. In-flight / Unknown Orders Resolution
        active_orders = self.repo.get_active_orders()
        for order in active_orders:
            if order.state in (OrderState.SUBMITTING, OrderState.UNKNOWN, OrderState.SENT):
                broker_order = self.broker.get_order(client_order_id=order.client_order_id)
                if broker_order and broker_order.success:
                    # Order berhasil diisi di broker!
                    self.oms.mark_ack(order.order_id, broker_order_id=broker_order.order_id)
                    self.oms.mark_filled(
                        order_id=order.order_id,
                        fill_price=broker_order.price,
                        filled_lot=broker_order.lot_size,
                        broker_order_id=broker_order.order_id
                    )
                    reports.append(DiscrepancyReport(
                        discrepancy_type=DiscrepancyType.MISSING_ACK_RESOLVED,
                        action_taken=DiscrepancyAction.ADOPT_AND_SYNC,
                        details={"client_order_id": order.client_order_id, "broker_order_id": broker_order.order_id}
                    ))
                    logger.info(f"RECONCILER: Resolved in-flight order {order.client_order_id} -> FILLED")

        # 4. Positions Reconciliation
        local_positions = {p.position_id: p for p in self.repo.get_open_positions()}
        broker_positions = {p.position_id: p for p in self.broker.get_positions()}

        # 4a. Detect Ghost Local Positions (ada di local DB, sudah lenyap di broker)
        for local_id, local_pos in local_positions.items():
            if local_id not in broker_positions:
                if self.policy.auto_close_ghost_positions:
                    local_pos.status = "CLOSED"
                    local_pos.closed_at = now
                    self.repo.upsert_position(local_pos)
                    reports.append(DiscrepancyReport(
                        discrepancy_type=DiscrepancyType.GHOST_LOCAL_POSITION,
                        action_taken=DiscrepancyAction.MARK_CLOSED_LOCAL,
                        details={"position_id": local_id, "symbol": local_pos.symbol}
                    ))
                    logger.info(f"RECONCILER: Closed ghost local position #{local_id}")

        # 4b. Detect Orphan or Unknown Broker Positions (ada di broker, belum ada di local DB)
        for broker_id, bpos in broker_positions.items():
            if broker_id not in local_positions:
                # Periksa apakah posisi milik bot kita
                is_known_bot = False
                if bpos.client_order_id and bpos.client_order_id.startswith(self.policy.expected_comment_prefix):
                    is_known_bot = True
                elif self.repo.get_order_by_client_id(bpos.client_order_id or ""):
                    is_known_bot = True

                if is_known_bot and self.policy.allow_auto_adopt_orphan:
                    adopted = PositionRecord(
                        position_id=broker_id,
                        client_order_id=bpos.client_order_id,
                        symbol=bpos.symbol,
                        action=bpos.action,
                        volume=bpos.volume,
                        entry_price=bpos.entry_price,
                        sl=bpos.sl,
                        tp=bpos.tp,
                        current_price=bpos.current_price,
                        unrealized_pnl=bpos.profit,
                        status="OPEN"
                    )
                    self.repo.upsert_position(adopted)
                    reports.append(DiscrepancyReport(
                        discrepancy_type=DiscrepancyType.ORPHAN_BROKER_POSITION,
                        action_taken=DiscrepancyAction.ADOPT_AND_SYNC,
                        details={"broker_position_id": broker_id, "symbol": bpos.symbol}
                    ))
                    logger.warning(f"RECONCILER: Adopted orphan bot position #{broker_id} into OMS")
                else:
                    # Posisi misterius dari manual intervention / external EA
                    if self.policy.lock_risk_on_unknown_external:
                        self.is_risk_locked = True
                        reports.append(DiscrepancyReport(
                            discrepancy_type=DiscrepancyType.UNKNOWN_EXTERNAL_POSITION,
                            action_taken=DiscrepancyAction.LOCK_NEW_RISK,
                            details={"broker_position_id": broker_id, "symbol": bpos.symbol, "volume": bpos.volume}
                        ))
                        logger.critical(
                            f"RECONCILER ALERT: Unknown external position #{broker_id} on {bpos.symbol}! "
                            "LOCKING NEW RISK to protect margin and daily drawdown."
                        )

        # 5. Compile Summary
        is_clean = len([r for r in reports if r.action_taken in (
            DiscrepancyAction.LOCK_NEW_RISK, DiscrepancyAction.EMERGENCY_HALT_AND_LIQUIDATE
        )]) == 0

        if is_clean and not self.is_risk_locked:
            self.is_risk_locked = False

        return ReconciliationSummary(
            is_clean=is_clean,
            risk_locked=self.is_risk_locked,
            discrepancies=reports
        )
