"""
Reconciliation Domain Models & Discrepancy Types.
Mendefinisikan klasifikasi selisih antara state internal OMS dan state nyata broker.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List


class DiscrepancyType(str, Enum):
    SYNCED = "SYNCED"
    MISSING_ACK_RESOLVED = "MISSING_ACK_RESOLVED"          # Order in-flight/unknown terisi di broker
    ORPHAN_BROKER_POSITION = "ORPHAN_BROKER_POSITION"      # Posisi bot di broker tapi belum ada di local OMS
    UNKNOWN_EXTERNAL_POSITION = "UNKNOWN_EXTERNAL_POSITION"# Posisi broker dari luar sistem / manual
    GHOST_LOCAL_POSITION = "GHOST_LOCAL_POSITION"          # Posisi di OMS masih OPEN tapi di broker sudah tidak ada
    EMERGENCY_LIMIT_BREACH = "EMERGENCY_LIMIT_BREACH"      # Drawdown equity broker melanggar limit prop firm


class DiscrepancyAction(str, Enum):
    NONE = "NONE"
    ADOPT_AND_SYNC = "ADOPT_AND_SYNC"
    MARK_CLOSED_LOCAL = "MARK_CLOSED_LOCAL"
    LOCK_NEW_RISK = "LOCK_NEW_RISK"
    EMERGENCY_HALT_AND_LIQUIDATE = "EMERGENCY_HALT_AND_LIQUIDATE"


@dataclass
class DiscrepancyReport:
    discrepancy_type: DiscrepancyType
    action_taken: DiscrepancyAction
    details: Dict[str, Any]
    timestamp_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ReconciliationSummary:
    is_clean: bool
    risk_locked: bool
    discrepancies: List[DiscrepancyReport] = field(default_factory=list)
    timestamp_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
