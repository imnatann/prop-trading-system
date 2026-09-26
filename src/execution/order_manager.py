"""
Order Execution Manager.
Mengelola pengiriman order atomik (dengan SL & TP), validasi akhir sebelum tembak, dan pencatatan riwayat.
"""

from typing import List, Optional
from loguru import logger

from src.strategy.base import TradeSignal
from .broker_adapter import BrokerAdapter, OrderResult


class OrderManager:
    """Manajer eksekusi order prop trading."""

    def __init__(self, broker: BrokerAdapter):
        self.broker = broker
        self.executed_orders: List[OrderResult] = []

    def dispatch_trade(self, signal: TradeSignal, lot_size: float) -> OrderResult:
        """
        Mengeksekusi order yang telah disetujui oleh Risk Gatekeeper.
        SL dan TP dikirim bersamaan secara atomik.
        """
        logger.info(
            f"OrderManager DISPATCHING: {signal.action.value} {lot_size} lot {signal.symbol} | "
            f"SL: {signal.stop_loss} | TP: {signal.take_profit}"
        )

        result = self.broker.execute_order(
            symbol=signal.symbol,
            action=signal.action,
            lot_size=lot_size,
            sl=signal.stop_loss,
            tp=signal.take_profit
        )

        if result.success:
            self.executed_orders.append(result)
            logger.info(f"OrderManager SUCCESS: Order #{result.order_id} filled at {result.price}")
        else:
            logger.error(f"OrderManager FAILED: {result.error_message}")

        return result

    def get_order_history(self) -> List[OrderResult]:
        return self.executed_orders
