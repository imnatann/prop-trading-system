"""
Startup Guard & Pre-Flight Security Gatekeeper.
Mencegah Trading Engine aktif jika ditemukan kondisi tidak aman saat reboot mesin:
- halt.lock aktif
- Split-brain (proses lain sedang berjalan)
- Kerusakan integritas database SQLite / Schema mismatch
- Broker disconnect
- Posisi terbuka tanpa Stop Loss (Naked Positions) atau floating drawdown kritis
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from loguru import logger

from config.prop_rules import PropFirmRules
from src.broker.base import BaseBrokerAdapter, PositionInfo
from src.oms.repository import OMSRepository
from src.safety.leader_lock import LeaderLock
from src.storage.health import DatabaseHealth
from src.storage.migration import DatabaseMigration


@dataclass
class StartupDecision:
    allowed: bool
    reason: str
    details: Dict[str, Any] = field(default_factory=dict)


class StartupGuard:
    """Penjaga gerbang inisialisasi sistem saat mesin atau layanan menyala."""

    def __init__(
        self,
        broker: BaseBrokerAdapter,
        repo: OMSRepository,
        rules: PropFirmRules,
        lock_file_path: str = "halt.lock",
        leader_lock: Optional[LeaderLock] = None,
        db_health: Optional[DatabaseHealth] = None,
        migration: Optional[DatabaseMigration] = None
    ):
        self.broker = broker
        self.repo = repo
        self.rules = rules
        self.lock_file = Path(lock_file_path)
        self.leader_lock = leader_lock or LeaderLock()
        self.db_health = db_health or DatabaseHealth(str(repo.db_path))
        self.migration = migration or DatabaseMigration(str(repo.db_path))

    def validate_open_positions(self, positions: List[PositionInfo]) -> Tuple[bool, str]:
        """
        Validasi risiko posisi terbuka yang tertinggal saat restart mesin:
        1. Setiap posisi WAJIB memiliki Stop Loss (DILARANG Naked Trade!).
        2. Sisa batas drawdown harian harus mencukupi.
        """
        if not positions:
            return True, "No open positions on broker (clean slate)"

        for p in positions:
            # 1. Pastikan posisi memiliki SL
            if p.sl <= 0.0:
                msg = f"NAKED POSITION DETECTED: Position #{p.position_id} ({p.symbol} {p.action} {p.volume} lot) has NO STOP LOSS!"
                logger.critical(f"StartupGuard: {msg}")
                return False, msg

        # 2. Cek apakah floating drawdown saat ini sudah melebihi buffer aman
        account = self.broker.get_account_snapshot()
        total_loss = account.balance - account.equity
        if total_loss > 0:
            loss_pct = (total_loss / account.balance) * 100.0
            if loss_pct >= (self.rules.max_daily_loss_pct * 0.8):
                msg = f"Startup blocked: Existing floating loss {loss_pct:.2f}% consumes >80% of daily limit ({self.rules.max_daily_loss_pct}%)"
                logger.critical(f"StartupGuard: {msg}")
                return False, msg

        return True, f"Verified {len(positions)} open positions: all have Stop Loss and safe margin"

    def evaluate_startup(self) -> StartupDecision:
        details: Dict[str, Any] = {}

        # 1. Cek Emergency Lock
        if self.lock_file.exists():
            msg = f"Startup BLOCKED: Emergency lock file '{self.lock_file}' is present. Manual intervention required."
            logger.critical(f"StartupGuard: {msg}")
            return StartupDecision(allowed=False, reason=msg, details={"halt_lock": True})

        # 2. Cek Split-Brain Leader Lock
        if not self.leader_lock.acquire():
            msg = "Startup BLOCKED: Another Trading Engine instance is already running (Split-Brain prevented)!"
            logger.critical(f"StartupGuard: {msg}")
            return StartupDecision(allowed=False, reason=msg, details={"split_brain": True})

        # 3. Cek Kesehatan & Integritas Database
        db_status = self.db_health.full_health_check()
        details["db_health"] = db_status
        if not db_status["is_healthy"]:
            self.leader_lock.release()
            msg = f"Startup BLOCKED: Database health check failed: {db_status.get('integrity_message') or db_status.get('disk_message')}"
            logger.critical(f"StartupGuard: {msg}")
            return StartupDecision(allowed=False, reason=msg, details=details)

        # 4. Cek Versi Skema Database
        compat_ok, compat_msg = self.migration.check_compatibility()
        details["schema_compatibility"] = compat_msg
        if not compat_ok:
            self.leader_lock.release()
            msg = f"Startup BLOCKED: Database schema incompatibility: {compat_msg}"
            logger.critical(f"StartupGuard: {msg}")
            return StartupDecision(allowed=False, reason=msg, details=details)

        # 5. Cek Koneksi & Kesehatan Broker
        b_health = self.broker.health()
        details["broker_health"] = b_health
        if not b_health.get("connected", False):
            self.leader_lock.release()
            msg = "Startup BLOCKED: Broker adapter is disconnected or unhealthy."
            logger.critical(f"StartupGuard: {msg}")
            return StartupDecision(allowed=False, reason=msg, details=details)

        # 6. Validasi Risiko Posisi Terbuka
        open_positions = self.broker.get_positions()
        pos_ok, pos_msg = self.validate_open_positions(open_positions)
        details["open_positions_check"] = pos_msg
        if not pos_ok:
            self.leader_lock.release()
            return StartupDecision(allowed=False, reason=pos_msg, details=details)

        logger.info("StartupGuard: ALL PRE-FLIGHT CHECKS PASSED. Trading authorized.")
        return StartupDecision(allowed=True, reason="Pre-flight checks passed", details=details)
