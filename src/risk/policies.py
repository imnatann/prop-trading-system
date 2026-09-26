"""
Prop Firm Policy Strategy Pattern.
Mengisolasi perbedaan aturan kalkulasi batas kerugian antar prop firm:
- Funding Pips (Primary Target)
- The5ers (Secondary Compatibility)
- FundedNext (Tertiary Compatibility)
"""

from typing import Optional, Protocol, runtime_checkable
from dataclasses import dataclass


@runtime_checkable
class PropFirmPolicy(Protocol):
    """Protokol resmi kebijakan aturan drawdown prop firm."""

    @property
    def name(self) -> str:
        ...

    @property
    def max_daily_loss_pct(self) -> float:
        ...

    @property
    def max_total_loss_pct(self) -> float:
        ...

    @property
    def kill_switch_pct(self) -> float:
        ...

    def compute_daily_baseline(self, balance: float, equity: float) -> float:
        """Menghitung titik acuan (baseline) harian pada pukul 00:00 server time."""
        ...

    def compute_daily_floor(self, baseline: float, initial_balance: float) -> float:
        """Menghitung batas terendah equity hari ini yang jika tersentuh dianggap breach."""
        ...

    def is_daily_breached(self, equity: float, baseline: float, initial_balance: float) -> bool:
        """Cek apakah equity saat ini menembus batas bawah harian."""
        ...

    def is_total_breached(self, equity: float, initial_balance: float,
                          balance: Optional[float] = None) -> bool:
        """Cek apakah equity ATAU balance melanggar batas maksimal akun."""
        ...

    def is_kill_switch_triggered(self, equity: float, baseline: float, initial_balance: float) -> bool:
        """Cek apakah menyentuh ambang batas likuidasi darurat."""
        ...


@dataclass(frozen=True)
class FundingPipsPolicy:
    """
    Aturan Resmi Funding Pips 2-Step Standard:
    - Daily Loss: 5% dari nilai tertinggi antara opening balance atau opening equity pada 00:00 platform time.
    - Maximum Loss: 10% static dari starting size.
    - Default buffer internal: 4.0% daily (kill switch 3.8%) dan 8.0% total.
    """
    name: str = "Funding Pips 2-Step Standard"
    max_daily_loss_pct: float = 4.0        # Buffer dari batas 5.0% broker
    max_total_loss_pct: float = 8.0        # Buffer dari batas 10.0% broker
    kill_switch_pct: float = 3.8           # Cutoff darurat
    broker_rule_daily_pct: float = 5.0

    def compute_daily_baseline(self, balance: float, equity: float) -> float:
        # Aturan Funding Pips: Higher of opening balance or equity
        return max(balance, equity)

    def compute_daily_floor(self, baseline: float, initial_balance: float) -> float:
        """Official base is the BASELINE, not the initial capital.

        FundingPips: "Equity cannot fall by more than 5% of that baseline at any
        point during the day", with the worked example baseline $107,000 ->
        5% = $5,350 -> floor $101,650. The previous implementation subtracted a
        percentage of the INITIAL balance from the baseline, which is a
        different (and smaller) number whenever equity is the higher value at
        the reset, so a simulated breach was not the broker's breach.
        """
        return baseline * (1.0 - (self.max_daily_loss_pct / 100.0))

    def is_daily_breached(self, equity: float, baseline: float, initial_balance: float) -> bool:
        floor = self.compute_daily_floor(baseline, initial_balance)
        return equity <= floor

    def is_total_breached(self, equity: float, initial_balance: float,
                          balance: Optional[float] = None) -> bool:
        """Official wording: "Equity OR BALANCE cannot hit 10% below starting".

        Testing equity alone was unsafe: a realised loss can drive balance below
        the floor while a still-open winner lifts equity above it, so the equity
        check would report "safe" on an account the broker has already closed.
        """
        for value in (equity, balance):
            if value is None:
                continue
            total_loss = initial_balance - value
            if total_loss <= 0:
                continue
            if (total_loss / initial_balance) * 100.0 >= self.max_total_loss_pct:
                return True
        return False

    def is_kill_switch_triggered(self, equity: float, baseline: float, initial_balance: float) -> bool:
        kill_floor = baseline * (1.0 - (self.kill_switch_pct / 100.0))
        return equity <= kill_floor


@dataclass(frozen=True)
class The5ersPolicy:
    """
    Aturan Resmi The5ers High Stakes Program:
    - Daily Loss: 5% dari higher of previous day closing balance/equity (Hard Breach).
    - Maximum Loss: 10% Trailing Drawdown.
    """
    name: str = "The5ers High Stakes"
    max_daily_loss_pct: float = 4.0
    max_total_loss_pct: float = 8.0
    kill_switch_pct: float = 3.8

    def compute_daily_baseline(self, balance: float, equity: float) -> float:
        return max(balance, equity)

    def compute_daily_floor(self, baseline: float, initial_balance: float) -> float:
        # The5ers persentase dihitung dari baseline itu sendiri
        return baseline * (1.0 - (self.max_daily_loss_pct / 100.0))

    def is_daily_breached(self, equity: float, baseline: float, initial_balance: float) -> bool:
        floor = self.compute_daily_floor(baseline, initial_balance)
        return equity <= floor

    def is_total_breached(self, equity: float, initial_balance: float) -> bool:
        total_loss = initial_balance - equity
        if total_loss <= 0:
            return False
        return (total_loss / initial_balance) * 100.0 >= self.max_total_loss_pct

    def is_kill_switch_triggered(self, equity: float, baseline: float, initial_balance: float) -> bool:
        kill_floor = baseline * (1.0 - (self.kill_switch_pct / 100.0))
        return equity <= kill_floor


@dataclass(frozen=True)
class FundedNextPolicy:
    """
    Aturan Resmi FundedNext Stellar 2-Step:
    - Daily Loss Limit: 5% nominal berbasis modal awal (Initial Balance).
    - Maximum Loss: 10% static.
    """
    name: str = "FundedNext Stellar 2-Step"
    max_daily_loss_pct: float = 4.0
    max_total_loss_pct: float = 8.0
    kill_switch_pct: float = 3.8

    def compute_daily_baseline(self, balance: float, equity: float) -> float:
        # FundedNext menggunakan opening balance hari tersebut
        return balance

    def compute_daily_floor(self, baseline: float, initial_balance: float) -> float:
        return baseline - (initial_balance * (self.max_daily_loss_pct / 100.0))

    def is_daily_breached(self, equity: float, baseline: float, initial_balance: float) -> bool:
        floor = self.compute_daily_floor(baseline, initial_balance)
        return equity <= floor

    def is_total_breached(self, equity: float, initial_balance: float,
                          balance: Optional[float] = None) -> bool:
        """Official wording: "Equity OR BALANCE cannot hit 10% below starting".

        Testing equity alone was unsafe: a realised loss can drive balance below
        the floor while a still-open winner lifts equity above it, so the equity
        check would report "safe" on an account the broker has already closed.
        """
        for value in (equity, balance):
            if value is None:
                continue
            total_loss = initial_balance - value
            if total_loss <= 0:
                continue
            if (total_loss / initial_balance) * 100.0 >= self.max_total_loss_pct:
                return True
        return False

    def is_kill_switch_triggered(self, equity: float, baseline: float, initial_balance: float) -> bool:
        kill_floor = baseline * (1.0 - (self.kill_switch_pct / 100.0))
        return equity <= kill_floor
