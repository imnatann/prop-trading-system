"""
Process Heartbeat Manager.
Menyediakan mekanisme penulisan dan pembacaan heartbeat berbasis file JSON antar-proses
untuk memonitor liveness trading engine dan risk watchdog.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from loguru import logger


class HeartbeatManager:
    """Manajer sinyal liveness/heartbeat antar proses."""

    def __init__(self, directory: str = "storage/heartbeats"):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _get_path(self, component_name: str) -> Path:
        return self.dir / f"{component_name}.json"

    def write_heartbeat(self, component_name: str, payload: Optional[Dict[str, Any]] = None) -> None:
        """Menulis timestamp heartbeat terkini dan PID proses."""
        path = self._get_path(component_name)
        now_utc = datetime.now(timezone.utc)
        data = {
            "component": component_name,
            "pid": os.getpid(),
            "timestamp_utc": now_utc.isoformat(),
            "timestamp_epoch": now_utc.timestamp(),
            "payload": payload or {}
        }
        # Atomic write via temp file
        temp_path = path.with_suffix(".tmp")
        try:
            with open(temp_path, "w") as f:
                json.dump(data, f, indent=2)
            temp_path.replace(path)
        except Exception as e:
            logger.error(f"HeartbeatManager: Failed to write heartbeat for {component_name}: {e}")

    def read_heartbeat(self, component_name: str) -> Optional[Dict[str, Any]]:
        path = self._get_path(component_name)
        if not path.exists():
            return None
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"HeartbeatManager: Failed to read heartbeat for {component_name}: {e}")
            return None

    def is_alive(self, component_name: str, max_stale_seconds: float = 10.0) -> bool:
        """Memeriksa apakah komponen masih hidup berdasarkan usia timestamp terakhir."""
        hb = self.read_heartbeat(component_name)
        if not hb or "timestamp_epoch" not in hb:
            return False
        age = datetime.now(timezone.utc).timestamp() - hb["timestamp_epoch"]
        return age <= max_stale_seconds
