"""
Unit Tests for News Blackout Filter.
Memvalidasi proteksi jeda waktu sebelum dan sesudah rilis berita High Impact.
"""

from datetime import datetime, timezone
import pytest
from src.data.news_calendar import EconomicEvent
from src.risk.news_filter import NewsFilter


def test_news_filter_blackout_detection():
    filter_engine = NewsFilter(minutes_before=5, minutes_after=5)

    # Event: US Non-Farm Payrolls pada 12:30 UTC
    nfp_event = EconomicEvent(
        title="Non-Farm Employment Change",
        currency="USD",
        impact="High",
        timestamp_utc=datetime(2026, 9, 20, 12, 30, tzinfo=timezone.utc)
    )
    events = [nfp_event]

    # 1. Tepat 3 menit sebelum rilis (12:27 UTC) -> Harus DIBLOKIR
    is_blocked, _ = filter_engine.is_in_news_blackout(
        symbol="EURUSD",
        events=events,
        check_time=datetime(2026, 9, 20, 12, 27, tzinfo=timezone.utc)
    )
    assert is_blocked is True

    # 2. Tepat 2 menit setelah rilis (12:32 UTC) -> Harus DIBLOKIR
    is_blocked, _ = filter_engine.is_in_news_blackout(
        symbol="EURUSD",
        events=events,
        check_time=datetime(2026, 9, 20, 12, 32, tzinfo=timezone.utc)
    )
    assert is_blocked is True

    # 3. 15 menit setelah rilis (12:45 UTC) -> Harus AMAN (LOLOS)
    is_blocked, _ = filter_engine.is_in_news_blackout(
        symbol="EURUSD",
        events=events,
        check_time=datetime(2026, 9, 20, 12, 45, tzinfo=timezone.utc)
    )
    assert is_blocked is False

    # 4. Pair yang tidak berhubungan (EURGBP tidak terpengaruh berita USD)
    is_blocked, _ = filter_engine.is_in_news_blackout(
        symbol="EURGBP",
        events=events,
        check_time=datetime(2026, 9, 20, 12, 28, tzinfo=timezone.utc)
    )
    assert is_blocked is False
