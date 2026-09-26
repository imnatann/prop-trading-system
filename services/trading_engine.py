"""
Trading Engine Service Daemon.
Proses utama eksekusi trading, OMS, dan signal processing.
Menulis heartbeat secara berkala ke storage/heartbeats/trading_engine.json.
"""

import time
from typing import Optional
from loguru import logger

from config.prop_rules import PropFirmRules
from src.execution.broker_adapter import MockBrokerAdapter, MT5BrokerAdapter
from src.execution.dispatcher import OrderDispatcher
from src.oms.repository import OMSRepository
from src.oms.service import OMSService
from src.reconciliation.reconciler import ReconciliationEngine
from src.risk.drawdown_monitor import DrawdownMonitor
from src.risk.gatekeeper import RiskGatekeeper
from src.safety.heartbeat import HeartbeatManager
from src.safety.kill_switch import EmergencyKillSwitch
from src.safety.leader_lock import LeaderLock
from src.safety.startup_guard import StartupGuard


class TradingEngineService:
    def __init__(
        self,
        broker=None,
        db_path: str = "storage/trading.db",
        heartbeat_dir: str = "storage/heartbeats",
        rules: Optional[PropFirmRules] = None,
        leader_lock_path: str = "runtime/trading_engine.lock",
        halt_lock_path: str = "halt.lock"
    ):
        self.rules = rules or PropFirmRules()
        self.broker = broker or MockBrokerAdapter()
        self.broker.connect()

        self.repo = OMSRepository(db_path=db_path)
        self.oms = OMSService(self.repo)

        self.drawdown_monitor = DrawdownMonitor(
            initial_balance=self.broker.balance,
            max_daily_loss_pct=self.rules.max_daily_loss_pct,
            max_total_loss_pct=self.rules.max_total_loss_pct
        )
        self.gatekeeper = RiskGatekeeper(self.rules, self.drawdown_monitor)
        self.dispatcher = OrderDispatcher(
            oms=self.oms,
            broker=self.broker,
            gatekeeper=self.gatekeeper
        )
        self.reconciler = ReconciliationEngine(
            oms=self.oms,
            broker=self.broker,
            drawdown_monitor=self.drawdown_monitor
        )
        self.heartbeat = HeartbeatManager(heartbeat_dir)
        self.kill_switch = EmergencyKillSwitch(self.broker, self.rules, self.drawdown_monitor)
        self.leader_lock = LeaderLock(lock_path=leader_lock_path)
        self.startup_guard = StartupGuard(
            broker=self.broker,
            repo=self.repo,
            rules=self.rules,
            lock_file_path=halt_lock_path,
            leader_lock=self.leader_lock
        )

    def startup(self) -> None:
        logger.info("TradingEngineService starting up...")
        # 0. Startup preflight security checks
        decision = self.startup_guard.evaluate_startup()
        if not decision.allowed:
            msg = f"StartupGuard rejected engine startup: {decision.reason}"
            logger.critical(msg)
            raise RuntimeError(msg)

        # 1. Startup reconciliation
        summary = self.reconciler.reconcile()
        logger.info(f"Startup reconciliation finished. Clean: {summary.is_clean}, Locked: {summary.risk_locked}")
        # 2. Write initial heartbeat
        self.heartbeat.write_heartbeat("trading_engine", {"status": "STARTUP_OK"})

    def tick_once(self) -> None:
        """Satu iterasi siklus kerja engine."""
        self.heartbeat.write_heartbeat("trading_engine", {"status": "RUNNING"})

        if self.kill_switch.is_locked():
            logger.warning("TradingEngine: System is locked by Emergency Kill Switch. Skipping signals.")
            return

        if self.reconciler.is_risk_locked:
            logger.warning("TradingEngine: Risk is locked by Reconciler. Running reconciliation check...")
            self.reconciler.reconcile()


def run_engine():
    service = TradingEngineService()
    service.startup()
    try:
        while True:
            service.tick_once()
            time.sleep(1.0)
    except KeyboardInterrupt:
        logger.info("TradingEngine stopped by user.")


if __name__ == "__main__":
    run_engine()
