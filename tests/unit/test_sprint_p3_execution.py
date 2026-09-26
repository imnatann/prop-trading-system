import os
import tempfile
import pytest
from datetime import datetime, timezone
from typing import Optional

from config.prop_rules import PropFirmRules
from src.broker.base import BaseBrokerAdapter, OrderResult, PositionInfo
from src.broker.models import SymbolSpec
from src.execution.dispatcher import OrderDispatcher
from src.execution.idempotency import IdempotencyRegistry, generate_client_order_id
from src.oms.repository import OMSRepository
from src.oms.service import OMSService
from src.oms.states import OrderState
from src.risk.drawdown_monitor import AccountSnapshot, DrawdownMonitor
from src.risk.gatekeeper import RiskGatekeeper
from src.strategy.base import SignalAction, TradeSignal


@pytest.fixture
def temp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    yield db_path
    for ext in ["", "-wal", "-shm"]:
        p = f"{db_path}{ext}"
        if os.path.exists(p):
            os.remove(p)


class FailingBrokerMock(BaseBrokerAdapter):
    """Mock broker untuk menyimulasikan network timeout atau disconnect."""

    def __init__(self, simulate_timeout: bool = True, order_succeeded_on_broker: bool = False):
        self.simulate_timeout = simulate_timeout
        self.order_succeeded_on_broker = order_succeeded_on_broker
        self._executed = {}

    def connect(self) -> bool:
        return True

    def disconnect(self) -> None:
        pass

    def health(self):
        return {"status": "HEALTHY"}

    def get_server_time(self):
        return datetime.now(timezone.utc)

    def get_account_snapshot(self):
        return AccountSnapshot(balance=100000.0, equity=100000.0, margin=0.0, free_margin=100000.0)

    def get_symbol_spec(self, symbol: str):
        return SymbolSpec(symbol=symbol, tick_size=0.00001, tick_value=1.0, contract_size=100000.0, volume_min=0.01, volume_max=50.0, volume_step=0.01, digits=5)

    def get_symbol_price(self, symbol: str):
        return 1.0850, 1.0851, 1.0

    def get_positions(self, symbol: Optional[str] = None):
        return []

    def get_open_positions_count(self, symbol: Optional[str] = None):
        return 0

    def execute_order(self, symbol, action, lot_size, sl, tp, client_order_id=None, comment=""):
        if self.simulate_timeout:
            if self.order_succeeded_on_broker and client_order_id:
                # Simulasikan skenario di mana broker memproses transaksi tapi koneksi putus sebelum ACK balik ke bot
                res = OrderResult(
                    success=True,
                    order_id="BROKER-RECOVERED-99",
                    client_order_id=client_order_id,
                    symbol=symbol,
                    action=action.value,
                    lot_size=lot_size,
                    price=1.0851,
                    sl=sl,
                    tp=tp,
                    status="FILLED",
                    timestamp_utc=datetime.now(timezone.utc)
                )
                self._executed[client_order_id] = res
            raise TimeoutError("Socket connection timeout after 5000ms")

        return OrderResult(
            success=True,
            order_id="MOCK-001",
            client_order_id=client_order_id,
            symbol=symbol,
            action=action.value,
            lot_size=lot_size,
            price=1.0851,
            sl=sl,
            tp=tp,
            status="FILLED",
            timestamp_utc=datetime.now(timezone.utc)
        )

    def get_order(self, client_order_id: Optional[str] = None, broker_order_id: Optional[str] = None):
        if client_order_id in self._executed:
            return self._executed[client_order_id]
        return None

    def cancel_order(self, order_id: str) -> bool:
        return True

    def close_position(self, position_id: str) -> bool:
        return True

    def close_all_positions(self) -> int:
        return 0


def test_idempotency_key_generation_and_registry():
    cid = generate_client_order_id("FP", "EURUSD")
    assert cid.startswith("QP-FP-")
    assert "EURUSD" in cid

    registry = IdempotencyRegistry()
    assert registry.acquire(cid) is True
    # Trying to acquire same ID again should be rejected
    assert registry.acquire(cid) is False

    registry.release_success(cid)
    # Even after release_success, same ID cannot be re-acquired
    assert registry.acquire(cid) is False
    assert registry.is_known(cid) is True


def test_order_dispatcher_happy_path(temp_db):
    from src.execution.broker_adapter import MockBrokerAdapter

    repo = OMSRepository(db_path=temp_db)
    oms = OMSService(repo)
    broker = MockBrokerAdapter(initial_balance=100_000.0)
    broker.connect()

    dispatcher = OrderDispatcher(oms=oms, broker=broker)

    signal = TradeSignal(
        symbol="EURUSD",
        action=SignalAction.BUY,
        entry_price=1.0850,
        stop_loss=1.0800,
        take_profit=1.0950,
        rationale="H1 Trend Confirmation"
    )

    result = dispatcher.dispatch(signal, lot_size=0.5)

    assert result.success is True
    assert result.lot_size == 0.5
    assert result.client_order_id is not None

    # Verify OMS record
    order = repo.get_order_by_client_id(result.client_order_id)
    assert order is not None
    assert order.state == OrderState.FILLED
    assert order.broker_order_id == result.order_id

    # Verify position in OMS
    positions = repo.get_open_positions()
    assert len(positions) == 1
    assert positions[0].volume == 0.5


def test_order_dispatcher_risk_gatekeeper_rejection(temp_db):
    from src.execution.broker_adapter import MockBrokerAdapter

    repo = OMSRepository(db_path=temp_db)
    oms = OMSService(repo)
    broker = MockBrokerAdapter(initial_balance=100_000.0)
    broker.connect()

    # Drawdown monitor breached
    rules = PropFirmRules()
    dd_monitor = DrawdownMonitor(initial_balance=100_000.0, max_daily_loss_pct=5.0)
    dd_monitor.reset_daily_baseline(100_000.0, 100_000.0, force=True)
    broker.set_mock_equity(94_000.0)  # 6% loss > 5% max daily dd

    gatekeeper = RiskGatekeeper(rules=rules, drawdown_monitor=dd_monitor)
    dispatcher = OrderDispatcher(oms=oms, broker=broker, gatekeeper=gatekeeper)

    signal = TradeSignal(
        symbol="EURUSD",
        action=SignalAction.BUY,
        entry_price=1.0850,
        stop_loss=1.0800,
        take_profit=1.0950,
        rationale="Test Breached Signal"
    )

    result = dispatcher.dispatch(signal, lot_size=0.5)

    assert result.success is False
    assert "Daily Drawdown breach" in (result.error_message or "")
    order = repo.get_order_by_client_id(result.client_order_id)
    assert order is not None
    assert order.state == OrderState.REJECTED
    assert len(broker.get_positions()) == 0


def test_order_dispatcher_timeout_to_unknown_without_blind_retry(temp_db):
    repo = OMSRepository(db_path=temp_db)
    oms = OMSService(repo)
    failing_broker = FailingBrokerMock(simulate_timeout=True, order_succeeded_on_broker=False)

    dispatcher = OrderDispatcher(oms=oms, broker=failing_broker)

    signal = TradeSignal(
        symbol="EURUSD",
        action=SignalAction.SELL,
        entry_price=1.0850,
        stop_loss=1.0900,
        take_profit=1.0750,
        rationale="Timeout Test"
    )

    result = dispatcher.dispatch(signal, lot_size=0.2)

    assert result.success is False
    assert result.status == "UNKNOWN"

    order = repo.get_order_by_client_id(result.client_order_id)
    assert order is not None
    assert order.state == OrderState.UNKNOWN


def test_order_dispatcher_recovery_query_success(temp_db):
    repo = OMSRepository(db_path=temp_db)
    oms = OMSService(repo)
    # Broker throws timeout exception during execute, but the order actually was created at broker
    recovering_broker = FailingBrokerMock(simulate_timeout=True, order_succeeded_on_broker=True)

    dispatcher = OrderDispatcher(oms=oms, broker=recovering_broker)

    signal = TradeSignal(
        symbol="EURUSD",
        action=SignalAction.BUY,
        entry_price=1.0850,
        stop_loss=1.0820,
        take_profit=1.0920,
        rationale="Recovery Test"
    )

    result = dispatcher.dispatch(signal, lot_size=0.3)

    assert result.success is True
    assert result.order_id == "BROKER-RECOVERED-99"

    order = repo.get_order_by_client_id(result.client_order_id)
    assert order is not None
    assert order.state == OrderState.FILLED
    assert order.broker_order_id == "BROKER-RECOVERED-99"
