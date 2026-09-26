"""
Prop Firm Rules & Guardrail Parameters.
Konfigurasi batas ketat evaluasi akun Prop Firm (FTMO, The5ers, Funding Pips, dll).
Dilengkapi preset profil resmi untuk setiap prop firm ternama.
"""

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class PropFirmRules:
    """Batas risiko hard limit yang tidak boleh dilanggar bot."""

    name: str = "Standard Conservative"

    # Drawdown limits (dalam persen %)
    # Catatan: Jika limit broker 5%, buffer bot di-set di 3.8% - 4.0% untuk menyerap slippage
    max_daily_loss_pct: float = 4.0        # Buffer batas rugi harian
    max_total_loss_pct: float = 8.0        # Buffer batas rugi total akun
    kill_switch_daily_pct: float = 3.8     # Cutoff kritis untuk emergency liquidation & lock
    
    # Position Sizing & Allocation
    risk_per_trade_pct: float = 0.5        # Risiko per posisi: 0.5% dari equity
    max_open_trades: int = 3               # Maksimal jumlah order aktif bersamaan
    max_total_risk_exposure_pct: float = 1.5 # Total risiko floating maksimal di portofolio
    
    # Execution & Market Gatekeepers
    max_spread_pips: float = 3.0           # Batas spread maksimal yang diizinkan untuk entry
    news_avoid_minutes_before: int = 5     # Menit sebelum high-impact news dilarang buka posisi
    news_avoid_minutes_after: int = 5      # Menit setelah high-impact news dilarang buka posisi
    
    # Duration & Holding Protection
    min_trade_duration_seconds: int = 60   # Anti-tick scalping rule (banyak prop firm melarang hold < 1m)
    force_close_friday_utc_hour: int = 21  # Tutup semua order pada Jumat jam 21:00 UTC (proteksi gap weekend)


# =====================================================================
# PRESET PROFIL RESMI PROP FIRM
# =====================================================================

FTMO_2STEP = PropFirmRules(
    name="FTMO 2-Step Challenge",
    max_daily_loss_pct=4.0,          # Aturan resmi: 5% dari modal awal. Buffer aman: 4.0%
    max_total_loss_pct=8.0,          # Aturan resmi: 10% static. Buffer aman: 8.0%
    kill_switch_daily_pct=3.8,
    risk_per_trade_pct=0.5,
    max_open_trades=3,
    news_avoid_minutes_before=3,     # Aturan FTMO Funded non-swing: 2 menit. Buffer: 3 menit
    news_avoid_minutes_after=3,
    min_trade_duration_seconds=60,
    force_close_friday_utc_hour=21
)

FUNDING_PIPS_2STEP = PropFirmRules(
    name="Funding Pips 2-Step",
    max_daily_loss_pct=4.0,          # Aturan resmi: 5% higher of balance/equity. Buffer: 4.0%
    max_total_loss_pct=8.0,          # Aturan resmi: 10% static. Buffer: 8.0%
    kill_switch_daily_pct=3.8,
    risk_per_trade_pct=0.4,          # Sizing lebih ketat karena midnight equity baseline
    max_open_trades=2,
    news_avoid_minutes_before=5,
    news_avoid_minutes_after=5,
    min_trade_duration_seconds=60,
    force_close_friday_utc_hour=21
)

THE5ERS_HIGH_STAKES = PropFirmRules(
    name="The5ers High Stakes",
    max_daily_loss_pct=4.0,          # Aturan resmi: 5% daily loss (hard breach). Buffer: 4.0%
    max_total_loss_pct=8.0,          # Aturan resmi: 10% trailing. Buffer: 8.0%
    kill_switch_daily_pct=3.8,
    risk_per_trade_pct=0.35,         # Sizing lebih aman untuk trailing drawdown
    max_open_trades=2,
    news_avoid_minutes_before=5,
    news_avoid_minutes_after=5,
    min_trade_duration_seconds=120,  # 2 minutes minimum hold rule
    force_close_friday_utc_hour=21
)

PROFILES: Dict[str, PropFirmRules] = {
    "DEFAULT": PropFirmRules(),
    "FTMO": FTMO_2STEP,
    "FUNDING_PIPS": FUNDING_PIPS_2STEP,
    "THE5ERS": THE5ERS_HIGH_STAKES,
}
