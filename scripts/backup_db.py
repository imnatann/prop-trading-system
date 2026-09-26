"""
Online SQLite Database Hot Backup Script.
Menggunakan 'VACUUM INTO' untuk backup non-blocking konsisten dengan retensi 30 hari (harian) dan 90 hari (mingguan).
Memverifikasi PRAGMA integrity_check pada database hasil backup.
"""

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from loguru import logger


def run_backup(
    db_path: str = "storage/trading.db",
    backup_dir: str = "storage/backups",
    daily_retention_days: int = 30,
    weekly_retention_days: int = 90
) -> bool:
    src_db = Path(db_path)
    if not src_db.exists():
        logger.warning(f"Backup skipped: Source database {db_path} does not exist yet.")
        return True

    b_dir = Path(backup_dir)
    b_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    is_sunday = (now.weekday() == 6)
    prefix = "weekly" if is_sunday else "daily"
    timestamp_str = now.strftime("%Y%m%d_%H%M%S")
    backup_filename = f"trading_{prefix}_{timestamp_str}.db"
    backup_path = b_dir / backup_filename

    logger.info(f"Starting online hot backup: {src_db} -> {backup_path}")

    # 1. Non-blocking atomic VACUUM INTO
    try:
        conn = sqlite3.connect(str(src_db), timeout=30.0)
        conn.execute(f"VACUUM INTO '{str(backup_path)}';")
        conn.close()
        logger.info(f"VACUUM INTO completed successfully: {backup_path} ({backup_path.stat().st_size} bytes)")
    except Exception as e:
        logger.critical(f"Backup failed during VACUUM INTO: {e}")
        if backup_path.exists():
            backup_path.unlink()
        return False

    # 2. Verify PRAGMA integrity_check on backup
    try:
        b_conn = sqlite3.connect(str(backup_path), timeout=10.0)
        cursor = b_conn.execute("PRAGMA integrity_check;")
        res = cursor.fetchall()
        b_conn.close()

        if res and res[0][0] == "ok":
            logger.info("Backup integrity check PASSED (ok).")
        else:
            logger.critical(f"Backup integrity check FAILED: {res}")
            backup_path.unlink()
            return False
    except Exception as e:
        logger.critical(f"Failed to verify backup integrity: {e}")
        if backup_path.exists():
            backup_path.unlink()
        return False

    # 3. Clean up expired backups according to retention policy
    _clean_retention(b_dir, now, daily_retention_days, weekly_retention_days)
    return True


def _clean_retention(backup_dir: Path, now: datetime, daily_days: int, weekly_days: int) -> None:
    for f in backup_dir.glob("trading_*.db"):
        mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        age_days = (now - mtime).days

        if "weekly" in f.name and age_days > weekly_days:
            logger.info(f"Purging expired weekly backup (> {weekly_days}d): {f.name}")
            f.unlink()
        elif "daily" in f.name and age_days > daily_days:
            logger.info(f"Purging expired daily backup (> {daily_days}d): {f.name}")
            f.unlink()


if __name__ == "__main__":
    success = run_backup()
    sys.exit(0 if success else 1)
