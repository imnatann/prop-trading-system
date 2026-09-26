"""
Out-of-Band Emergency Kill Switch Daemon.
Proses pengaman independen yang memantau floating drawdown akun secara berkala.
Jika menyentuh batas kritis (hard cutoff), menutup seluruh posisi dan mengunci sistem dengan halt.lock.
"""

import os
from pathlib import Path
from loguru import logger

from config.prop_rules import PropFirmRules
from src.risk.drawdown_monitor import DrawdownMonitor
from src.execution.broker_adapter import BrokerAdapter


class EmergencyKillSwitch:
    """Pengaman darurat akun prop firm independen."""

    def __init__(
        self,
        broker: BrokerAdapter,
        rules: PropFirmRules,
        drawdown_monitor: DrawdownMonitor,
        lock_file_path: str = "halt.lock"
    ):
        self.broker = broker
        self.rules = rules
        self.drawdown_monitor = drawdown_monitor
        self.lock_file_path = Path(lock_file_path)

    def is_locked(self) -> bool:
        """Cek apakah sistem sedang terkunci oleh kill switch."""
        return self.lock_file_path.exists()

    def engage_lock(self, reason: str) -> None:
        """Mengunci sistem ke file halt.lock dan menutup seluruh posisi aktif."""
        logger.critical(f"EMERGENCY KILL SWITCH TRIGGERED: {reason}")
        
        # 1. Tulis lock file permanen
        self.lock_file_path.write_text(f"LOCKED: {reason}\nTimestamp: {logger.datetime.now() if hasattr(logger, 'datetime') else 'NOW'}")
        
        # 2. Liquidate seluruh posisi di broker
        closed_count = self.broker.close_all_positions()
        logger.critical(f"Liquidated {closed_count} open positions. System LOCKED until manual intervention.")

    def release_lock(self) -> bool:
        """Membuka kunci sistem (hanya boleh dipanggil setelah intervensi manusia/hari baru)."""
        if self.lock_file_path.exists():
            self.lock_file_path.unlink()
            logger.info("Emergency lock released successfully.")
            return True
        return False

    def monitor_and_enforce(self) -> bool:
        """
        Pemeriksaan kondisi darurat.
        Returns: True jika sistem aman, False jika kill switch terpicu.
        """
        if self.is_locked():
            logger.warning("System is currently LOCKED by Emergency Kill Switch. All trades prohibited.")
            return False

        account = self.broker.get_account_snapshot()
        self.drawdown_monitor.update_snapshot(account)

        daily_loss_pct = self.drawdown_monitor.calculate_daily_loss_pct(account.equity)
        total_loss_pct = self.drawdown_monitor.calculate_total_loss_pct(account.equity)

        # Cek apakah drawdown harian menyentuh batas kritis darurat
        if daily_loss_pct >= self.rules.kill_switch_daily_pct:
            reason = f"Daily Loss reached critical {daily_loss_pct:.2f}% (Threshold: {self.rules.kill_switch_daily_pct:.2f}%)"
            self.engage_lock(reason)
            return False

        # Cek apakah total loss menyentuh batas akun
        if total_loss_pct >= self.rules.max_total_loss_pct:
            reason = f"Total Loss reached critical {total_loss_pct:.2f}% (Threshold: {self.rules.max_total_loss_pct:.2f}%)"
            self.engage_lock(reason)
            return False

        return True
