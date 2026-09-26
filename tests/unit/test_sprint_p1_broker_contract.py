"""
Unit Tests for Sprint P1 — Broker Contract Complete Interface.
Memvalidasi seluruh kapabilitas interface BaseBrokerAdapter pada MockBrokerAdapter.
"""

from datetime import datetime
import pytest

from src.execution.broker_adapter import MockBrokerAdapter
from src.strategy.base import SignalAction


def test_mock_broker_contract_lifecycle():
    broker = MockBrokerAdapter(initial_balance=100_000.0)

    # 1. Connect & Health
    assert broker.connect() is True
    h = broker.health()
    assert h["status"] == "HEALTHY"
    assert h["connected"] is True
    assert h["latency_ms"] > 0

    # 2. Server time & Symbol Spec
    srv_time = broker.get_server_time()
    assert isinstance(srv_time, datetime)
    spec_eur = broker.get_symbol_spec("EURUSD")
    assert spec_eur.symbol == "EURUSD"
    assert spec_eur.tick_size == 0.00001
    assert spec_eur.digits == 5

    # 3. Execute Order dengan Client Order ID
    client_oid = "QP-FP-20260920-EURUSD-001"
    res = broker.execute_order(
        symbol="EURUSD",
        action=SignalAction.BUY,
        lot_size=1.5,
        sl=1.08200,
        tp=1.09000,
        client_order_id=client_oid
    )
    assert res.success is True
    assert res.client_order_id == client_oid
    assert res.status == "FILLED"
    assert broker.get_open_positions_count() == 1

    # 4. Get Positions & Filter by Symbol
    positions = broker.get_positions(symbol="EURUSD")
    assert len(positions) == 1
    pos = positions[0]
    assert pos.symbol == "EURUSD"
    assert pos.volume == 1.5
    assert pos.client_order_id == client_oid

    # 5. Query Order by Client Order ID and Broker Order ID
    ord_by_client = broker.get_order(client_order_id=client_oid)
    assert ord_by_client is not None
    assert ord_by_client.order_id == res.order_id

    ord_by_broker = broker.get_order(broker_order_id=res.order_id)
    assert ord_by_broker is not None
    assert ord_by_broker.client_order_id == client_oid

    # 6. Close single position
    closed = broker.close_position(pos.position_id)
    assert closed is True
    assert broker.get_open_positions_count() == 0

    # 7. Disconnect
    broker.disconnect()
    h_disc = broker.health()
    assert h_disc["connected"] is False
    assert h_disc["status"] == "DISCONNECTED"
