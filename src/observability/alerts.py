"""
Alerting Engine & Debounce Dispatcher.
Mengirimkan notifikasi darurat (Drawdown Warnings, Emergency Liquidation, Disconnects)
dengan mekanisme anti-spam / debouncing.
"""

from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional
from loguru import logger


class AlertManager:
    """Manajer notifikasi dengan rate limiting / debounce per topik."""

    def __init__(self, debounce_seconds: float = 60.0):
        self.debounce_seconds = debounce_seconds
        self._last_alert_times: Dict[str, float] = {}
        self._handlers: List[Callable[[str, str, str], None]] = []

    def register_handler(self, handler: Callable[[str, str, str], None]) -> None:
        """Mendaftarkan handler webhook / telegram / callback."""
        self._handlers.append(handler)

    def trigger_alert(self, topic: str, severity: str, message: str) -> bool:
        """
        Mengirim alert jika sudah melampaui masa cooldown debounce.
        Returns: True jika terkirim, False jika di-suppress oleh debounce.
        """
        now_epoch = datetime.now(timezone.utc).timestamp()
        last_time = self._last_alert_times.get(topic, 0.0)

        if now_epoch - last_time < self.debounce_seconds:
            logger.debug(f"AlertManager: Suppressed duplicate alert '{topic}' (cooldown active)")
            return False

        self._last_alert_times[topic] = now_epoch

        log_msg = f"ALERT [{severity.upper()}][{topic}]: {message}"
        if severity.upper() == "CRITICAL":
            logger.critical(log_msg)
        elif severity.upper() == "WARNING":
            logger.warning(log_msg)
        else:
            logger.info(log_msg)

        # Dispatch ke seluruh handler
        for handler in self._handlers:
            try:
                handler(topic, severity, message)
            except Exception as e:
                logger.error(f"Alert handler exception: {e}")

        return True
