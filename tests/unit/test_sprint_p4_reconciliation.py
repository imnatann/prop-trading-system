import os
import tempfile
import pytest
from datetime import datetime, timezone

from config.prop_rules import PropFirmRules
from src.broker.base import OrderResult, PositionInfo
from src.execution.broker_adapter import MockBrokerAdapter
from src.oms.models import PositionRecord
from src.oms.repository import OMSRepository
from src.oms.service import OMSService
from src.oms.states import OrderState
from src.reconciliation.models import DiscrepancyAction, DiscrepancyType
from src.reconciliation.policies import ReconciliationPolicy
from src.reconciliation.reconciler import ReconciliationEngine
from src.risk.drawdown_monitor import DrawdownMonitor


@pytest.fixture
def temp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    yield db_path
    for ext in ["", "-wal", "-shm"]:
        p = f"{db_path}{ext}"
        if os.path.exists(p):
            os.remove(p)


def test_reconciliation_clean_state(temp_db):
    repo = OMSRepository(db_path=temp_db)
    oms = OMSService(repo)
    broker = MockBrokerAdapter(initial_balance=100_000.0)
    broker.connect()

    reconciler = ReconciliationEngine(oms=oms, broker=broker)
    summary = reconciler.reconcile()

    assert summary.is_clean is True
    assert summary.risk_locked is False
    assert len(summary.discrepancies) == 0


def test_reconciliation_in_flight_unknown_order_resolved(temp_db):
    repo = OMSRepository(db_path=temp_db)
    oms = OMSService(repo)
    broker = MockBrokerAdapter(initial_balance=100_000.0)
    broker.connect()

    # Create an order in OMS and mark UNKNOWN
    order = oms.create_order("QP-FP-20260920-EURUSD-UNK1", "EURUSD", "BUY", 0.5, 1.0800, 1.0950)
    oms.mark_submitting(order.order_id)
    oms.mark_unknown(order.order_id, reason="Timeout")

    # Manually populate broker with the executed order and open position
    broker._executed_orders["QP-FP-20260920-EURUSD-UNK1"] = OrderResult(
        success=True,
        order_id="BROKER-TICKET-77",
        client_order_id="QP-FP-20260920-EURUSD-UNK1",
        symbol="EURUSD",
        action="BUY",
        lot_size=0.5,
        price=1.0850,
        sl=1.0800,
        tp=1.0950,
        status="FILLED",
        timestamp_utc=datetime.now(timezone.utc)
    )
    broker.positions.append(PositionInfo(
        position_id="BROKER-TICKET-77",
        client_order_id="QP-FP-20260920-EURUSD-UNK1",
        symbol="EURUSD",
        action="BUY",
        volume=0.5,
        entry_price=1.0850,
        sl=1.0800,
        tp=1.0950,
        current_price=1.0850,
        profit=0.0,
        open_time=datetime.now(timezone.utc)
    ))

    reconciler = ReconciliationEngine(oms=oms, broker=broker)
    summary = reconciler.reconcile()

    assert summary.is_clean is True
    assert summary.risk_locked is False
    assert len(summary.discrepancies) == 1
    assert summary.discrepancies[0].discrepancy_type == DiscrepancyType.MISSING_ACK_RESOLVED

    # Order in OMS is now FILLED
    updated_order = repo.get_order(order.order_id)
    assert updated_order.state == OrderState.FILLED
    assert updated_order.broker_order_id == "BROKER-TICKET-77"


def test_reconciliation_ghost_local_position_closed(temp_db):
    repo = OMSRepository(db_path=temp_db)
    oms = OMSService(repo)
    broker = MockBrokerAdapter(initial_balance=100_000.0)
    broker.connect()

    # Local DB thinks position is OPEN
    ghost_pos = PositionRecord(
        position_id="GHOST-POS-1",
        client_order_id="QP-FP-GHOST",
        symbol="EURUSD",
        action="BUY",
        volume=0.5,
        entry_price=1.0850,
        sl=1.0800,
        tp=1.0950,
        status="OPEN"
    )
    repo.upsert_position(ghost_pos)
    assert len(repo.get_open_positions()) == 1

    # Broker has NO open positions
    reconciler = ReconciliationEngine(oms=oms, broker=broker)
    summary = reconciler.reconcile()

    assert summary.is_clean is True
    assert len(summary.discrepancies) == 1
    assert summary.discrepancies[0].discrepancy_type == DiscrepancyType.GHOST_LOCAL_POSITION
    assert summary.discrepancies[0].action_taken == DiscrepancyAction.MARK_CLOSED_LOCAL

    # Verify OMS position is now closed
    assert len(repo.get_open_positions()) == 0


def test_reconciliation_orphan_broker_position_adopted(temp_db):
    repo = OMSRepository(db_path=temp_db)
    oms = OMSService(repo)
    broker = MockBrokerAdapter(initial_balance=100_000.0)
    broker.connect()

    # Broker has an open position belonging to the bot (starts with QP-)
    broker.positions.append(PositionInfo(
        position_id="BROKER-POS-88",
        client_order_id="QP-FP-ORPHAN-88",
        symbol="EURUSD",
        action="BUY",
        volume=0.3,
        entry_price=1.0860,
        sl=1.0810,
        tp=1.0960,
        current_price=1.0865,
        profit=15.0,
        open_time=datetime.now(timezone.utc)
    ))

    # Local OMS has no open positions
    assert len(repo.get_open_positions()) == 0

    reconciler = ReconciliationEngine(oms=oms, broker=broker)
    summary = reconciler.reconcile()

    assert summary.is_clean is True
    assert len(summary.discrepancies) == 1
    assert summary.discrepancies[0].discrepancy_type == DiscrepancyType.ORPHAN_BROKER_POSITION
    assert summary.discrepancies[0].action_taken == DiscrepancyAction.ADOPT_AND_SYNC

    # OMS now has adopted position
    open_pos = repo.get_open_positions()
    assert len(open_pos) == 1
    assert open_pos[0].position_id == "BROKER-POS-88"
    assert open_pos[0].volume == 0.3


def test_reconciliation_unknown_external_position_locks_risk(temp_db):
    repo = OMSRepository(db_path=temp_db)
    oms = OMSService(repo)
    broker = MockBrokerAdapter(initial_balance=100_000.0)
    broker.connect()

    # Manual position opened by human on mobile MT5 without QP- prefix
    broker.positions.append(PositionInfo(
        position_id="MANUAL-TRADER-99",
        client_order_id="MANUAL-MOBILE-APP",
        symbol="XAUUSD",
        action="SELL",
        volume=2.0,
        entry_price=2650.0,
        sl=2660.0,
        tp=2630.0,
        current_price=2652.0,
        profit=-40.0,
        open_time=datetime.now(timezone.utc)
    ))

    reconciler = ReconciliationEngine(oms=oms, broker=broker)
    summary = reconciler.reconcile()

    # Must FAIL-CLOSED: lock new risk
    assert summary.is_clean is False
    assert summary.risk_locked is True
    assert reconciler.is_risk_locked is True
    assert len(summary.discrepancies) == 1
    assert summary.discrepancies[0].discrepancy_type == DiscrepancyType.UNKNOWN_EXTERNAL_POSITION
    assert summary.discrepancies[0].action_taken == DiscrepancyAction.LOCK_NEW_RISK


def test_reconciliation_emergency_drawdown_breach_liquidation(temp_db):
    repo = OMSRepository(db_path=temp_db)
    oms = OMSService(repo)
    broker = MockBrokerAdapter(initial_balance=100_000.0)
    broker.connect()

    # Add 2 positions
    broker.positions.append(PositionInfo(
        position_id="POS-1", client_order_id="QP-1", symbol="EURUSD", action="BUY",
        volume=0.5, entry_price=1.0850, sl=1.0800, tp=1.0950, current_price=1.0850, profit=0.0, open_time=datetime.now(timezone.utc)
    ))
    broker.positions.append(PositionInfo(
        position_id="POS-2", client_order_id="QP-2", symbol="GBPUSD", action="BUY",
        volume=0.5, entry_price=1.2650, sl=1.2600, tp=1.2750, current_price=1.2650, profit=0.0, open_time=datetime.now(timezone.utc)
    ))

    # Drop equity to 93,000 (7% drawdown > 5% max daily drawdown)
    broker.set_mock_equity(93_000.0)

    rules = PropFirmRules()
    dd_monitor = DrawdownMonitor(initial_balance=100_000.0, max_daily_loss_pct=5.0)
    dd_monitor.reset_daily_baseline(100_000.0, 100_000.0, force=True)

    reconciler = ReconciliationEngine(oms=oms, broker=broker, drawdown_monitor=dd_monitor)
    summary = reconciler.reconcile()

    assert summary.is_clean is False
    assert summary.risk_locked is True
    assert summary.discrepancies[0].action_taken == DiscrepancyAction.EMERGENCY_HALT_AND_LIQUIDATE
    # All broker positions liquidated immediately!
    assert len(broker.positions) == 0
