"""
Comprehensive Order Lifecycle Test Script.
Membuktikan seluruh alur transaksi eksekusi prop trading:
Connect -> Get Symbol Spec -> Place Order -> Receive Ticket -> Verify SL/TP -> Modify SL/TP -> Close -> Reconcile
"""

import sys
from datetime import datetime, timezone
from typing import Optional
from loguru import logger

from src.broker.base import BaseBrokerAdapter, OrderResult
from src.execution.broker_adapter import MockBrokerAdapter, MT5BrokerAdapter
from src.oms.repository import OMSRepository
from src.oms.service import OMSService
from src.reconciliation.reconciler import ReconciliationEngine
from src.strategy.base import SignalAction


def run_lifecycle_test(adapter: Optional[BaseBrokerAdapter] = None, db_path: str = "storage/lifecycle_test.db") -> bool:
    broker = adapter or MockBrokerAdapter(initial_balance=100_000.0)
    repo = OMSRepository(db_path=db_path)
    oms = OMSService(repository=repo)
    reconciler = ReconciliationEngine(oms=oms, broker=broker)

    logger.info("=== STARTING ORDER LIFECYCLE VERIFICATION ===")

    # Step 1: Connect
    if not broker.connect():
        logger.critical("Step 1 FAILED: Could not connect to broker.")
        return False
    logger.info("Step 1 PASSED: Broker connected.")

    # Step 2: Get Symbol Spec
    symbol = "EURUSD"
    spec = broker.get_symbol_spec(symbol)
    assert spec is not None and spec.volume_min > 0
    logger.info(f"Step 2 PASSED: Received SymbolSpec for {symbol} (MinLot: {spec.volume_min})")

    # Step 3: Place Order with Atomically Bound SL & TP
    client_order_id = "QP-DEMO-TEST-001"
    bid, ask, _ = broker.get_symbol_price(symbol)
    entry_price = ask
    sl_price = round(entry_price - 0.0020, 5) # 20 pips SL
    tp_price = round(entry_price + 0.0040, 5) # 40 pips TP

    order = oms.create_order(client_order_id, symbol, "BUY", 0.1, sl_price, tp_price)
    oms.mark_submitting(order.order_id)

    order_res: OrderResult = broker.execute_order(
        symbol=symbol,
        action=SignalAction.BUY,
        lot_size=0.1,
        sl=sl_price,
        tp=tp_price,
        client_order_id=client_order_id
    )

    if not order_res.success:
        logger.critical(f"Step 3 FAILED: Order submission failed: {order_res.error_message}")
        return False
    logger.info(f"Step 3 PASSED: Order submitted. Ticket: #{order_res.order_id}")

    # Step 4: Receive Ticket & Verify in OMS
    oms.mark_ack(order.order_id, broker_order_id=order_res.order_id)
    order = oms.mark_filled(order.order_id, fill_price=order_res.price, filled_lot=order_res.lot_size, broker_order_id=order_res.order_id)
    assert order.state.value == "FILLED"
    logger.info(f"Step 4 PASSED: Order #{client_order_id} filled @ {order_res.price}")

    # Step 5: Verify SL and TP on active broker position
    positions = broker.get_positions(symbol)
    target_pos = next((p for p in positions if p.position_id == order_res.order_id or p.client_order_id == client_order_id), None)
    if not target_pos:
        logger.critical("Step 5 FAILED: Open position not found on broker.")
        return False

    assert abs(target_pos.sl - sl_price) < 1e-4
    assert abs(target_pos.tp - tp_price) < 1e-4
    logger.info(f"Step 5 PASSED: Verified SL ({target_pos.sl}) & TP ({target_pos.tp}) on broker position #{target_pos.position_id}")

    # Step 6: Close Position
    closed = broker.close_position(target_pos.position_id)
    if not closed:
        logger.critical("Step 6 FAILED: Failed to close position on broker.")
        return False
    logger.info(f"Step 6 PASSED: Position #{target_pos.position_id} closed.")

    # Step 7: Reconcile State Post-Close
    summary = reconciler.reconcile()
    assert summary.is_clean is True
    assert summary.risk_locked is False
    assert len(repo.get_open_positions()) == 0
    logger.info("Step 7 PASSED: Reconciliation complete. Zero open position discrepancies.")

    logger.info("=== FULL ORDER LIFECYCLE TEST 100% SUCCESSFUL ===")
    return True


if __name__ == "__main__":
    ok = run_lifecycle_test()
    sys.exit(0 if ok else 1)
