"""
Structured Audit Trail Logger.
Mencatat seluruh keputusan risiko, mutasi order, dan aksi keamanan
secara terstruktur untuk audit kepatuhan prop firm.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from loguru import logger

from src.oms.repository import OMSRepository


@dataclass
class AuditRecord:
    category: str                       # RISK, EXECUTION, RECONCILIATION, SAFETY
    severity: str                       # INFO, WARNING, CRITICAL
    action: str
    details: Dict[str, Any]
    timestamp_utc: datetime


class AuditLogger:
    """Pencatat audit log persisten untuk verifikasi aturan prop firm."""

    def __init__(self, repo: Optional[OMSRepository] = None):
        self.repo = repo

    def log(
        self,
        category: str,
        severity: str,
        action: str,
        details: Dict[str, Any]
    ) -> AuditRecord:
        now = datetime.now(timezone.utc)
        record = AuditRecord(
            category=category.upper(),
            severity=severity.upper(),
            action=action,
            details=details,
            timestamp_utc=now
        )

        msg = f"AUDIT [{record.category}][{record.severity}] {record.action} | {record.details}"
        if record.severity == "CRITICAL":
            logger.critical(msg)
        elif record.severity == "WARNING":
            logger.warning(msg)
        else:
            logger.info(msg)

        # Simpan ke SQLite jika repo tersedia
        if self.repo:
            try:
                with self.repo._get_connection() as conn:
                    conn.execute("""
                    INSERT INTO system_events (event_type, severity, message, timestamp_utc)
                    VALUES (?, ?, ?, ?)
                    """, (record.category, record.severity, f"{record.action}: {str(record.details)}", now.isoformat()))
                    conn.commit()
            except Exception as e:
                logger.error(f"AuditLogger failed to write to DB: {e}")

        return record
