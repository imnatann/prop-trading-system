"""
Out-of-Band Risk Watchdog.
Proses independen pengawas integritas yang beroperasi di luar trading engine.
Memonitor liveness engine, membaca langsung equity broker, dan mengambil tindakan protektif
jika engine mati saat ada posisi terbuka atau jika terjadi drawdown darurat.
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional
from loguru import logger

from config.prop_rules import PropFirmRules
from src.broker.base import BaseBrokerAdapter
from src.risk.drawdown_monitor import DrawdownMonitor
from src.safety.heartbeat import HeartbeatManager
from src.safety.kill_switch import EmergencyKillSwitch


@dataclass
class WatchdogCheckResult:
    healthy: bool
    engine_alive: bool
    equity_safe: bool
    action_taken: str
    details: Dict[str, Any]


class RiskWatchdog:
    """Watchdog pengawas risiko dan liveness proses trading."""

    def __init__(
        self,
        broker: BaseBrokerAdapter,
        rules: PropFirmRules,
        drawdown_monitor: DrawdownMonitor,
        heartbeat_manager: Optional[HeartbeatManager] = None,
        kill_switch: Optional[EmergencyKillSwitch] = None,
        max_stale_heartbeat_seconds: float = 15.0,
        auto_liquidate_on_dead_engine: bool = True
    ):
        self.broker = broker
        self.rules = rules
        self.drawdown_monitor = drawdown_monitor
        self.heartbeat = heartbeat_manager or HeartbeatManager()
        self.kill_switch = kill_switch or EmergencyKillSwitch(broker, rules, drawdown_monitor)
        self.max_stale_seconds = max_stale_heartbeat_seconds
        self.auto_liquidate_on_dead_engine = auto_liquidate_on_dead_engine

    def poll_once(self) -> WatchdogCheckResult:
        """
        Menjalankan 1 siklus inspeksi keamanan independen:
        1. Cek liveness trading engine via heartbeat file.
        2. Baca langsung equity & open positions dari broker.
        3. Evaluasi drawdown darurat.
        4. Tentukan tindakan pengamanan jika terjadi anomaly.
        """
        engine_alive = self.heartbeat.is_alive("trading_engine", max_stale_seconds=self.max_stale_seconds)
        open_pos_count = self.broker.get_open_positions_count()

        account = self.broker.get_account_snapshot()
        self.drawdown_monitor.update_snapshot(account)

        daily_loss_pct = self.drawdown_monitor.calculate_daily_loss_pct(account.equity)
        total_loss_pct = self.drawdown_monitor.calculate_total_loss_pct(account.equity)

        equity_safe = (
            daily_loss_pct < self.rules.kill_switch_daily_pct and
            total_loss_pct < self.rules.max_total_loss_pct
        )

        details = {
            "daily_loss_pct": daily_loss_pct,
            "total_loss_pct": total_loss_pct,
            "open_positions": open_pos_count,
            "equity": account.equity
        }

        # Skenario 1: Pelanggaran drawdown kritis -> Emergency Kill Switch
        if not equity_safe:
            reason = f"Critical Drawdown breach in watchdog: Daily {daily_loss_pct:.2f}%, Total {total_loss_pct:.2f}%"
            self.kill_switch.engage_lock(reason)
            return WatchdogCheckResult(
                healthy=False,
                engine_alive=engine_alive,
                equity_safe=False,
                action_taken="EMERGENCY_KILL_SWITCH",
                details=details
            )

        # Skenario 2: Trading Engine tewas/hang saat posisi terbuka -> Orphan protection
        if not engine_alive and open_pos_count > 0:
            logger.critical(
                f"WATCHDOG ALERT: Trading Engine is UNRESPONSIVE (stale > {self.max_stale_seconds}s) "
                f"while {open_pos_count} open positions remain on broker!"
            )
            if self.auto_liquidate_on_dead_engine:
                closed = self.broker.close_all_positions()
                self.kill_switch.engage_lock("Engine unresponsive with open positions")
                details["liquidated_count"] = closed
                return WatchdogCheckResult(
                    healthy=False,
                    engine_alive=False,
                    equity_safe=True,
                    action_taken="ORPHAN_LIQUIDATE_AND_HALT",
                    details=details
                )
            else:
                return WatchdogCheckResult(
                    healthy=False,
                    engine_alive=False,
                    equity_safe=True,
                    action_taken="ALERT_ONLY",
                    details=details
                )

        # Skenario 3: Engine tidak aktif tapi tidak ada posisi terbuka
        if not engine_alive and open_pos_count == 0:
            return WatchdogCheckResult(
                healthy=True,
                engine_alive=False,
                equity_safe=True,
                action_taken="NONE_IDLE",
                details=details
            )

        # Skenario 4: Normal dan sehat
        self.heartbeat.write_heartbeat("risk_watchdog", {"status": "HEALTHY"})
        return WatchdogCheckResult(
            healthy=True,
            engine_alive=True,
            equity_safe=True,
            action_taken="NONE",
            details=details
        )
