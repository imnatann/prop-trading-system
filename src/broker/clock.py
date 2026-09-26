"""
Broker Clock Abstraction & Server Time Management.
Menangani sinkronisasi jam broker, pergantian hari kalender platform (00:00 Server Time),
dan transisi Daylight Saving Time (DST) menggunakan zoneinfo.
"""

from datetime import datetime, date, timezone
from typing import Protocol, runtime_checkable
import zoneinfo
from loguru import logger


@runtime_checkable
class BrokerClock(Protocol):
    """Protokol jam broker yang dapat diinjeksi ke komponen pengawas risiko."""

    def now(self) -> datetime:
        """Mengembalikan datetime saat ini dalam timezone server broker."""
        ...

    def today(self) -> date:
        """Mengembalikan tanggal hari ini sesuai kalender server broker."""
        ...

    def is_new_day(self, last_recorded_date: date) -> bool:
        """Mengecek apakah server broker telah melewati batas tengah malam (00:00)."""
        ...


class ServerTimeClock:
    """
    Jam server broker berbasis ZoneInfo.
    Mayoritas broker prop firm MT5 (FundingPips, FTMO, The5ers) beroperasi di waktu
    Eastern European Time (EET/EEST, biasanya 'Europe/Nicosia' atau 'Europe/Prague')
    yang secara otomatis berganti antara UTC+2 (Musim Dingin) dan UTC+3 (Musim Panas).
    """

    def __init__(self, timezone_name: str = "Europe/Nicosia"):
        self.timezone_name = timezone_name
        try:
            self.tz = zoneinfo.ZoneInfo(timezone_name)
        except Exception as e:
            logger.warning(f"Timezone '{timezone_name}' tidak ditemukan, fallback ke UTC: {e}")
            self.tz = timezone.utc

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def today(self) -> date:
        return self.now().date()

    def is_new_day(self, last_recorded_date: date) -> bool:
        return self.today() > last_recorded_date


class MockBrokerClock:
    """
    Mock Clock untuk pengujian deterministik (Unit Tests & Simulations).
    Memungkinkan simulasi lompatan waktu, pengujian midnight rollover, dan pergantian hari.
    """

    def __init__(self, initial_datetime: datetime):
        self._current_time = initial_datetime

    def now(self) -> datetime:
        return self._current_time

    def today(self) -> date:
        return self._current_time.date()

    def is_new_day(self, last_recorded_date: date) -> bool:
        return self.today() > last_recorded_date

    def set_time(self, new_datetime: datetime) -> None:
        self._current_time = new_datetime

    def advance_minutes(self, minutes: int) -> None:
        from datetime import timedelta
        self._current_time += timedelta(minutes=minutes)

    def advance_hours(self, hours: int) -> None:
        from datetime import timedelta
        self._current_time += timedelta(hours=hours)

    def advance_days(self, days: int) -> None:
        from datetime import timedelta
        self._current_time += timedelta(days=days)
