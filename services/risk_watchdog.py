"""
Risk Watchdog Service Daemon.
Proses out-of-band independen yang mengawasi integritas akun dan liveness trading engine.
"""

import time
from typing import Optional
from loguru import logger

from config.prop_rules import PropFirmRules
from src.execution.broker_adapter import MockBrokerAdapter, MT5BrokerAdapter
from src.risk.drawdown_monitor import DrawdownMonitor
from src.safety.heartbeat import HeartbeatManager
from src.safety.kill_switch import EmergencyKillSwitch
from src.safety.watchdog import RiskWatchdog, WatchdogCheckResult


class RiskWatchdogService:
    def __init__(
        self,
        broker=None,
        heartbeat_dir: str = "storage/heartbeats",
        rules: Optional[PropFirmRules] = None
    ):
        self.rules = rules or PropFirmRules()
        self.broker = broker or MockBrokerAdapter()
        self.broker.connect()

        self.drawdown_monitor = DrawdownMonitor(
            initial_balance=self.broker.balance,
            max_daily_loss_pct=self.rules.max_daily_loss_pct,
            max_total_loss_pct=self.rules.max_total_loss_pct
        )
        self.heartbeat = HeartbeatManager(heartbeat_dir)
        self.kill_switch = EmergencyKillSwitch(self.broker, self.rules, self.drawdown_monitor)
        self.watchdog = RiskWatchdog(
            broker=self.broker,
            rules=self.rules,
            drawdown_monitor=self.drawdown_monitor,
            heartbeat_manager=self.heartbeat,
            kill_switch=self.kill_switch
        )

    def poll_once(self) -> WatchdogCheckResult:
        return self.watchdog.poll_once()


def run_watchdog():
    service = RiskWatchdogService()
    logger.info("RiskWatchdogService started.")
    try:
        while True:
            res = service.poll_once()
            if not res.healthy:
                logger.error(f"Watchdog detected anomaly: action={res.action_taken}")
            time.sleep(2.0)
    except KeyboardInterrupt:
        logger.info("RiskWatchdog stopped by user.")


if __name__ == "__main__":
    run_watchdog()
