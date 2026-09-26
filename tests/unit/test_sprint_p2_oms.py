import os
import tempfile
import pytest
from src.oms.states import OrderState, OrderEventType
from src.oms.repository import OMSRepository
from src.oms.service import OMSService


@pytest.fixture
def temp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    yield db_path
    if os.path.exists(db_path):
        os.remove(db_path)
    wal_file = f"{db_path}-wal"
    shm_file = f"{db_path}-shm"
    if os.path.exists(wal_file):
        os.remove(wal_file)
    if os.path.exists(shm_file):
        os.remove(shm_file)


def test_oms_order_lifecycle_fill_happy_path(temp_db):
    repo = OMSRepository(db_path=temp_db)
    oms = OMSService(repository=repo)

    # 1. Create Order
    client_order_id = "QP-FP-20260920-EURUSD-001"
    order = oms.create_order(
        client_order_id=client_order_id,
        symbol="EURUSD",
        action="BUY",
        requested_lot=0.5,
        sl_price=1.0800,
        tp_price=1.0950
    )
    assert order.state == OrderState.CREATED
    assert order.requested_lot == 0.5

    # 2. Risk Approved
    order = oms.mark_risk_approved(order.order_id, payload={"max_daily_dd_ok": True})
    assert order.state == OrderState.RISK_APPROVED

    # 3. Mark Submitting (Right before network IO)
    order = oms.mark_submitting(order.order_id)
    assert order.state == OrderState.SUBMITTING

    # 4. Mark Sent
    order = oms.mark_sent(order.order_id)
    assert order.state == OrderState.SENT

    # 5. Mark Ack
    order = oms.mark_ack(order.order_id, broker_order_id="MT5-998811")
    assert order.state == OrderState.ACKNOWLEDGED
    assert order.broker_order_id == "MT5-998811"

    # 6. Mark Filled
    order = oms.mark_filled(order.order_id, fill_price=1.0850, filled_lot=0.5, broker_order_id="MT5-998811")
    assert order.state == OrderState.FILLED
    assert order.filled_lot == 0.5

    # Verify positions recorded
    open_positions = repo.get_open_positions()
    assert len(open_positions) == 1
    pos = open_positions[0]
    assert pos.symbol == "EURUSD"
    assert pos.volume == 0.5
    assert pos.entry_price == 1.0850
    assert pos.status == "OPEN"

    # Verify audit event trail
    events = repo.get_order_events(order.order_id)
    assert len(events) == 6
    assert events[0].event_type == OrderEventType.ORDER_CREATED
    assert events[1].event_type == OrderEventType.RISK_APPROVED
    assert events[2].event_type == OrderEventType.BROKER_SUBMIT
    assert events[3].event_type == OrderEventType.BROKER_SUBMIT
    assert events[4].event_type == OrderEventType.BROKER_ACK
    assert events[5].event_type == OrderEventType.FILL


def test_oms_order_rejection_path(temp_db):
    repo = OMSRepository(db_path=temp_db)
    oms = OMSService(repository=repo)

    order = oms.create_order(
        client_order_id="QP-FP-20260920-EURUSD-REJ",
        symbol="EURUSD",
        action="SELL",
        requested_lot=1.0,
        sl_price=1.0900,
        tp_price=1.0700
    )
    oms.mark_submitting(order.order_id)
    order = oms.mark_rejected(order.order_id, reason="Broker: Off quotes / high slippage")

    assert order.state == OrderState.REJECTED
    active = repo.get_active_orders()
    assert len(active) == 0


def test_oms_order_timeout_unknown_state(temp_db):
    repo = OMSRepository(db_path=temp_db)
    oms = OMSService(repository=repo)

    order = oms.create_order(
        client_order_id="QP-FP-20260920-EURUSD-TIMEOUT",
        symbol="EURUSD",
        action="BUY",
        requested_lot=0.2,
        sl_price=1.0800,
        tp_price=1.0900
    )
    oms.mark_submitting(order.order_id)
    order = oms.mark_unknown(order.order_id, reason="Socket timeout after 5000ms")

    assert order.state == OrderState.UNKNOWN
    # UNKNOWN is not in terminal states (FILLED, CANCELLED, REJECTED, EXPIRED), so it remains in active orders for reconciliation
    active = repo.get_active_orders()
    assert len(active) == 1
    assert active[0].order_id == order.order_id


def test_oms_persistence_durability_across_restarts(temp_db):
    # Process 1 creates data
    repo1 = OMSRepository(db_path=temp_db)
    oms1 = OMSService(repository=repo1)

    order1 = oms1.create_order("QP-FP-RESTART-001", "EURUSD", "BUY", 0.1, 1.0800, 1.0900)
    oms1.mark_filled(order1.order_id, fill_price=1.0855, filled_lot=0.1, broker_order_id="BROKER-101")

    order2 = oms1.create_order("QP-FP-RESTART-002", "EURUSD", "SELL", 0.2, 1.0900, 1.0750)
    oms1.mark_submitting(order2.order_id)

    # Simulate crash / reboot by destroying repo1 and instantiating repo2
    del oms1
    del repo1

    repo2 = OMSRepository(db_path=temp_db)
    recovered_order1 = repo2.get_order(order1.order_id)
    assert recovered_order1 is not None
    assert recovered_order1.state == OrderState.FILLED
    assert recovered_order1.broker_order_id == "BROKER-101"

    recovered_order2 = repo2.get_order_by_client_id("QP-FP-RESTART-002")
    assert recovered_order2 is not None
    assert recovered_order2.state == OrderState.SUBMITTING

    # Check open positions
    open_positions = repo2.get_open_positions()
    assert len(open_positions) == 1
    assert open_positions[0].client_order_id == "QP-FP-RESTART-001"
    assert open_positions[0].volume == 0.1
