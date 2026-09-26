"""
Economic Calendar & News Event Feed.
Mengambil dan memetakan jadwal rilis berita ekonomi berdampak tinggi (High Impact / Red Folder).
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional
import requests
from loguru import logger


@dataclass
class EconomicEvent:
    title: str
    currency: str
    impact: str          # "High", "Medium", "Low"
    timestamp_utc: datetime
    forecast: Optional[str] = None
    previous: Optional[str] = None


class NewsCalendar:
    """Fetcher & storage kalender berita ekonomi dengan cache lokal."""

    def __init__(self, high_impact_only: bool = True):
        self.high_impact_only = high_impact_only
        self._events: List[EconomicEvent] = []

    def load_mock_events(self, events: List[EconomicEvent]) -> None:
        """Memuat daftar event langsung (berguna untuk testing dan simulasi)."""
        self._events = events
        logger.debug(f"Loaded {len(events)} mock economic events")

    def fetch_calendar(self) -> List[EconomicEvent]:
        """
        Fetch kalender berita ekonomi mingguan.
        Default menggunakan endpoint JSON feed publik atau fallback ke memory cache.
        """
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        try:
            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                raw_data = response.json()
                parsed_events = []
                for item in raw_data:
                    impact = item.get("impact", "")
                    if self.high_impact_only and impact.lower() != "high":
                        continue

                    # Parse ISO format timestamp
                    date_str = item.get("date")
                    try:
                        event_time = datetime.fromisoformat(date_str).astimezone(timezone.utc)
                    except Exception:
                        continue

                    event = EconomicEvent(
                        title=item.get("title", "Unknown Event"),
                        currency=item.get("country", "").upper(),
                        impact=impact,
                        timestamp_utc=event_time,
                        forecast=item.get("forecast"),
                        previous=item.get("previous"),
                    )
                    parsed_events.append(event)

                self._events = parsed_events
                logger.info(f"Successfully fetched {len(self._events)} high-impact economic events.")
                return self._events
            else:
                logger.warning(f"Failed to fetch economic calendar: HTTP {response.status_code}")
        except Exception as e:
            logger.warning(f"Error connecting to economic calendar feed: {e}. Using cached events.")

        return self._events

    def get_events_for_currency(self, currency: str) -> List[EconomicEvent]:
        """Filter event berdasarkan mata uang tertentu (misal: USD, EUR)."""
        curr = currency.upper()
        return [ev for ev in self._events if ev.currency == curr]

    def get_all_events(self) -> List[EconomicEvent]:
        return self._events
