"""
Split-Brain Protection & Process Leader Lock.
Mencegah dua proses Trading Engine berjalan bersamaan dan bertransaksi saling mendahului.
Dilengkapi metadata PID, timestamp mulai, dan heartbeat untuk auto-recovery jika proses mati mendadak.
"""

import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from loguru import logger


class LeaderLock:
    """Kunci kepemimpinan proses trading eksklusif."""

    def __init__(
        self,
        lock_path: str = "runtime/trading_engine.lock",
        stale_threshold_seconds: float = 30.0
    ):
        self.lock_path = Path(lock_path)
        self.stale_threshold_seconds = stale_threshold_seconds
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._is_held = False

    def _is_pid_alive(self, pid: int) -> bool:
        """Memeriksa apakah PID masih hidup di sistem operasi."""
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False
        except Exception:
            return False

    def acquire(self) -> bool:
        """
        Mencoba mengunci kepemimpinan.
        Mengembalikan True jika sukses.
        Mengembalikan False jika ada proses lain yang aktif (Split-Brain dicegah).
        """
        if self._is_held:
            return True

        now = datetime.now(timezone.utc)

        if self.lock_path.exists():
            try:
                with open(self.lock_path, "r") as f:
                    meta = json.load(f)

                existing_pid = meta.get("pid")
                last_hb_str = meta.get("last_heartbeat")

                if existing_pid and self._is_pid_alive(existing_pid):
                    # Proses lain masih aktif dan terbukti hidup
                    logger.critical(
                        f"LeaderLock: Split-brain blocked! Active Trading Engine already running with PID {existing_pid} "
                        f"(Started at {meta.get('started_at')})."
                    )
                    return False
                else:
                    # Proses pemilik lock sudah mati / crash
                    logger.warning(
                        f"LeaderLock: Stale lock detected (PID {existing_pid} is dead). Recovering lock file."
                    )
            except Exception as e:
                logger.warning(f"LeaderLock: Corrupt lock file detected ({e}). Overwriting with new lock.")

        # Tulis metadata kepemimpinan baru
        new_meta = {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "started_at": now.isoformat(),
            "last_heartbeat": now.isoformat()
        }

        try:
            temp_path = self.lock_path.with_suffix(".tmp")
            with open(temp_path, "w") as f:
                json.dump(new_meta, f, indent=2)
            temp_path.replace(self.lock_path)
            self._is_held = True
            logger.info(f"LeaderLock: Successfully acquired by PID {os.getpid()}")
            return True
        except Exception as e:
            logger.error(f"LeaderLock: Failed to write lock file: {e}")
            return False

    def update_heartbeat(self) -> None:
        """Memperbarui timestamp heartbeat kunci kepemimpinan."""
        if not self._is_held or not self.lock_path.exists():
            return
        try:
            with open(self.lock_path, "r") as f:
                meta = json.load(f)
            meta["last_heartbeat"] = datetime.now(timezone.utc).isoformat()
            temp_path = self.lock_path.with_suffix(".tmp")
            with open(temp_path, "w") as f:
                json.dump(meta, f, indent=2)
            temp_path.replace(self.lock_path)
        except Exception as e:
            logger.error(f"LeaderLock: Heartbeat update failed: {e}")

    def release(self) -> bool:
        """Melepaskan kunci saat proses shutdown normal."""
        if self.lock_path.exists():
            try:
                self.lock_path.unlink()
                self._is_held = False
                logger.info("LeaderLock: Released successfully.")
                return True
            except Exception as e:
                logger.error(f"LeaderLock: Failed to delete lock file: {e}")
        return False

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("Could not acquire LeaderLock: another trading engine instance is running!")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
