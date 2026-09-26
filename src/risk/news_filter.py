"""
News Event Blackout Filter.
Mencegah eksekusi order pada jendela waktu rilis berita ekonomi High Impact.
"""

from datetime import datetime, timezone, timedelta
from typing import List, Tuple
from loguru import logger
from src.data.news_calendar import EconomicEvent


class NewsFilter:
    """Filter proteksi rilis berita ekonomi prop firm."""

    def __init__(self, minutes_before: int = 5, minutes_after: int = 5):
        self.minutes_before = minutes_before
        self.minutes_after = minutes_after

    @staticmethod
    def extract_currencies_from_symbol(symbol: str) -> List[str]:
        """Mengekstrak mata uang dari pair (misal: EURUSD -> ['EUR', 'USD'])."""
        sym = symbol.upper()
        if len(sym) == 6:
            return [sym[:3], sym[3:]]
        elif "XAU" in sym or "GOLD" in sym:
            return ["USD"]
        return [sym]

    def is_in_news_blackout(
        self,
        symbol: str,
        events: List[EconomicEvent],
        check_time: datetime = None
    ) -> Tuple[bool, str]:
        """
        Cek apakah saat ini berada dalam rentang terlarang trading berita.
        Returns: (is_blackout, reason)
        """
        if check_time is None:
            check_time = datetime.now(timezone.utc)
        elif check_time.tzinfo is None:
            check_time = check_time.replace(tzinfo=timezone.utc)

        pair_currencies = self.extract_currencies_from_symbol(symbol)

        for event in events:
            if event.currency in pair_currencies and event.impact.lower() == "high":
                blackout_start = event.timestamp_utc - timedelta(minutes=self.minutes_before)
                blackout_end = event.timestamp_utc + timedelta(minutes=self.minutes_after)

                if blackout_start <= check_time <= blackout_end:
                    reason = (
                        f"High-Impact News '{event.title}' ({event.currency}) at {event.timestamp_utc.strftime('%H:%M UTC')}. "
                        f"Blackout active from {blackout_start.strftime('%H:%M')} to {blackout_end.strftime('%H:%M')}."
                    )
                    return True, reason

        return False, "Clear"
