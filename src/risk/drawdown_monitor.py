"""
Drawdown Monitor & High-Water Mark Tracker.
Memantau drawdown harian dan drawdown keseluruhan sesuai aturan prop firm.
Mendukung injeksi BrokerClock (ZoneInfo) dan PropFirmPolicy (Strategy Pattern).
"""

from dataclasses import dataclass
from datetime import datetime, timezone, date
from typing import Optional
from loguru import logger

from src.broker.clock import BrokerClock, ServerTimeClock
from src.risk.policies import PropFirmPolicy, FundingPipsPolicy


@dataclass
class AccountSnapshot:
    balance: float
    equity: float
    margin: float = 0.0
    free_margin: float = 0.0
    timestamp_utc: Optional[datetime] = None

    def __post_init__(self):
        if self.timestamp_utc is None:
            self.timestamp_utc = datetime.now(timezone.utc)


class DrawdownMonitor:
    """
    Monitor kepatuhan batasan rugi akun prop firm.
    Mendukung reset harian pada pergantian hari server sesuai kebijakan firm.
    """

    def __init__(
        self,
        initial_balance: float,
        max_daily_loss_pct: Optional[float] = None,
        max_total_loss_pct: Optional[float] = None,
        kill_switch_pct: Optional[float] = None,
        policy: Optional[PropFirmPolicy] = None,
        clock: Optional[BrokerClock] = None,
        server_timezone_offset_hours: int = 2
    ):
        self.initial_balance = initial_balance
        self.clock: BrokerClock = clock or ServerTimeClock("Europe/Nicosia")
        
        # Jika policy spesifik tidak diberikan, gunakan FundingPipsPolicy (Primary Target)
        if policy is not None:
            self.policy: PropFirmPolicy = policy
        else:
            # Gunakan parameter override jika disediakan untuk backward-compatibility
            daily_pct = max_daily_loss_pct if max_daily_loss_pct is not None else 4.0
            total_pct = max_total_loss_pct if max_total_loss_pct is not None else 8.0
            kill_pct = kill_switch_pct if kill_switch_pct is not None else 3.8
            self.policy = FundingPipsPolicy(
                max_daily_loss_pct=daily_pct,
                max_total_loss_pct=total_pct,
                kill_switch_pct=kill_pct
            )

        self.max_daily_loss_pct = self.policy.max_daily_loss_pct
        self.max_total_loss_pct = self.policy.max_total_loss_pct
        self.kill_switch_pct = self.policy.kill_switch_pct

        self.start_of_day_balance = initial_balance
        self.start_of_day_equity = initial_balance
        self.start_of_day_baseline = initial_balance
        self.peak_balance = initial_balance
        
        # Gunakan tanggal dari kalender clock broker
        self.current_day_date: date = self.clock.today()

    def reset_daily_baseline(self, new_balance: float, current_equity: float, force: bool = False) -> None:
        """
        Dipanggil setiap pergantian hari server broker (00:00 Server Time).
        Menghitung baseline harian via PropFirmPolicy.
        """
        if force or self.clock.is_new_day(self.current_day_date):
            self.start_of_day_balance = new_balance
            self.start_of_day_equity = current_equity
            self.start_of_day_baseline = self.policy.compute_daily_baseline(new_balance, current_equity)
            self.current_day_date = self.clock.today()
            logger.info(
                f"[{self.policy.name}] Daily Drawdown baseline reset to ${self.start_of_day_baseline:,.2f} "
                f"(Balance: ${new_balance:,.2f}, Equity: ${current_equity:,.2f}) for server date: {self.current_day_date}"
            )

    def update_snapshot(self, snapshot: AccountSnapshot) -> None:
        """Update posisi saldo dan cek apakah ada hari baru."""
        self.reset_daily_baseline(snapshot.balance, snapshot.equity)
        if snapshot.balance > self.peak_balance:
            self.peak_balance = snapshot.balance

    def calculate_daily_loss_pct(self, current_equity: float) -> float:
        """
        Menghitung drawdown harian prop firm:
        Daily Loss % = ((StartOfDay_Baseline - Current_Equity) / StartOfDay_Baseline) * 100
        """
        if self.start_of_day_baseline <= 0:
            return 0.0
        
        drawdown_val = self.start_of_day_baseline - current_equity
        if drawdown_val <= 0:
            return 0.0
        return (drawdown_val / self.start_of_day_baseline) * 100.0

    def calculate_total_loss_pct(self, current_equity: float) -> float:
        """
        Menghitung total drawdown dari saldo awal akun.
        Total Loss % = ((Initial_Balance - Current_Equity) / Initial_Balance) * 100
        """
        if self.initial_balance <= 0:
            return 0.0

        drawdown_val = self.initial_balance - current_equity
        if drawdown_val <= 0:
            return 0.0
        return (drawdown_val / self.initial_balance) * 100.0

    def get_daily_floor(self) -> float:
        """Mengembalikan nilai floor equity harian dari policy."""
        return self.policy.compute_daily_floor(self.start_of_day_baseline, self.initial_balance)

    def is_daily_limit_breached(self, current_equity: float) -> bool:
        """Cek apakah daily drawdown menyentuh batas bawah harian sesuai policy."""
        return self.policy.is_daily_breached(current_equity, self.start_of_day_baseline, self.initial_balance)

    def is_total_limit_breached(self, current_equity: float,
                                current_balance: Optional[float] = None) -> bool:
        """Cek apakah total drawdown menyentuh batas akun.

        FundingPips wording is "equity OR balance cannot hit 10% below the
        starting size". Passing the balance is optional only for backward
        compatibility with callers that have no open position (where the two are
        equal); live callers should pass it.
        """
        return self.policy.is_total_breached(
            current_equity, self.initial_balance, balance=current_balance)

    def is_kill_switch_triggered(self, current_equity: float) -> bool:
        """Cek apakah menyentuh ambang batas darurat emergency liquidation."""
        return self.policy.is_kill_switch_triggered(current_equity, self.start_of_day_baseline, self.initial_balance)
