"""
Unified Clock Provider & Market Session Resolver.
Menghubungkan waktu sistem lokal, waktu server broker dinamis, penentuan tanggal rollover 00:00,
dan pendeteksian sesi pasar Forex (Asian, London, New York).
"""

from datetime import date, datetime, timezone
from typing import Optional, Tuple
from zoneinfo import ZoneInfo
from loguru import logger


class ClockProvider:
    """Penyedia waktu seragam dan pemetaan sesi trading prop firm."""

    def __init__(self, broker_timezone: str = "Europe/Nicosia"):
        self.broker_tz_name = broker_timezone
        try:
            self.broker_tz = ZoneInfo(broker_timezone)
        except Exception as e:
            logger.warning(f"ClockProvider: Invalid timezone '{broker_timezone}', falling back to UTC: {e}")
            self.broker_tz = timezone.utc

    def system_time(self) -> datetime:
        """Waktu sistem mesin/VPS dalam UTC."""
        return datetime.now(timezone.utc)

    def broker_time(self) -> datetime:
        """Waktu server broker yang telah dikonversi ke zona waktu spesifik firm."""
        return datetime.now(self.broker_tz)

    def trading_day(self) -> date:
        """Tanggal kalender aktif menurut server broker (titik jangkar rollover 00:00)."""
        return self.broker_time().date()

    def market_session(self, now_utc: Optional[datetime] = None) -> str:
        """
        Menentukan sesi pasar Forex aktif berdasarkan jam UTC:
        - London / NY Overlap: 13:00 - 16:00 UTC (Likuiditas tertinggi)
        - London: 08:00 - 16:00 UTC
        - New York: 13:00 - 21:00 UTC
        - Asian: 00:00 - 08:00 UTC (Tokyo / Sydney)
        - Rollover / Low Liquidity: 21:00 - 23:59 UTC
        """
        dt = now_utc or self.system_time()
        hour = dt.hour

        if 13 <= hour < 16:
            return "LONDON_NY_OVERLAP"
        elif 8 <= hour < 16:
            return "LONDON"
        elif 13 <= hour < 21:
            return "NEW_YORK"
        elif 0 <= hour < 8:
            return "ASIAN"
        else:
            return "ROLLOVER_OFF_HOURS"

    def validate_clock_drift(
        self,
        reported_broker_time: Optional[datetime] = None,
        max_drift_seconds: float = 2.0
    ) -> Tuple[bool, float, str]:
        """
        Memverifikasi selisih waktu (clock drift) antara waktu lokal mesin dan waktu broker.
        Jika drift > max_drift_seconds, return False (Clock Drift Anomaly!).
        """
        if reported_broker_time is None:
            return True, 0.0, "No external clock reference provided, drift check passed"

        expected = self.broker_time()
        # Normalisasi ke timezone yang sama
        if reported_broker_time.tzinfo is None:
            rep_tz = reported_broker_time.replace(tzinfo=self.broker_tz)
        else:
            rep_tz = reported_broker_time.astimezone(self.broker_tz)

        drift = abs((expected - rep_tz).total_seconds())
        if drift > max_drift_seconds:
            msg = f"Clock drift anomaly detected: {drift:.2f}s exceeds limit {max_drift_seconds:.2f}s"
            logger.critical(f"ClockProvider: {msg}")
            return False, drift, msg

        return True, drift, f"Clock synchronized: drift {drift:.2f}s <= {max_drift_seconds:.2f}s"
