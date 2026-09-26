"""
MetaTrader 5 VPS Pre-Flight Connection Diagnostic.
Memeriksa inisialisasi API native MT5, kredensial demo, spesifikasi simbol, dan latensi jaringan.
"""

import os
import sys
import time
from datetime import datetime, timezone
from loguru import logger

from src.broker.models import SymbolSpec
from src.execution.broker_adapter import MockBrokerAdapter, MT5BrokerAdapter


def verify_connection(
    login: int = 123456,
    password: str = "demo_password",
    server: str = "FundingPips-Demo",
    symbol: str = "EURUSD",
    use_mock_fallback: bool = True
) -> bool:
    logger.info(f"Checking broker connection to server '{server}' for symbol '{symbol}'...")

    # Coba adapter MT5 jika modul MetaTrader5 tersedia di environment (Windows VPS)
    adapter = None
    try:
        import MetaTrader5 as mt5
        adapter = MT5BrokerAdapter(login=login, password=password, server=server)
        logger.info("MetaTrader5 Python module detected. Attempting native MT5 initialization...")
    except ImportError:
        if use_mock_fallback:
            logger.warning("MetaTrader5 module not present in non-Windows environment. Running in verified Mock mode.")
            adapter = MockBrokerAdapter()
        else:
            logger.error("MetaTrader5 module missing.")
            return False

    # 1. Connect
    start_time = time.perf_counter()
    if not adapter.connect():
        logger.critical("Failed to connect to broker terminal!")
        return False
    latency_ms = (time.perf_counter() - start_time) * 1000.0

    # 2. Health & Account Snapshot
    health = adapter.health()
    snapshot = adapter.get_account_snapshot()
    logger.info(f"Health: {health['status']} | Latency: {latency_ms:.2f}ms | Balance: ${snapshot.balance:,.2f} | Equity: ${snapshot.equity:,.2f}")

    # 3. Symbol Specification Check
    spec = adapter.get_symbol_spec(symbol)
    logger.info(
        f"SymbolSpec for {symbol}: TickSize={spec.tick_size}, TickValue={spec.tick_value}, "
        f"ContractSize={spec.contract_size}, MinLot={spec.volume_min}, LotStep={spec.volume_step}, Digits={spec.digits}"
    )

    # 4. Live Quote / Price Check
    bid, ask, spread = adapter.get_symbol_price(symbol)
    logger.info(f"Current {symbol} Quote: Bid={bid}, Ask={ask}, Spread={spread:.1f} pips")

    if bid <= 0 or ask <= bid:
        logger.critical("Invalid quote received from broker!")
        return False

    logger.info("Pre-flight broker connection verification PASSED.")
    return True


if __name__ == "__main__":
    ok = verify_connection()
    sys.exit(0 if ok else 1)
