"""
Market Data Validation & Freshness Gatekeeper.
Mencegah eksekusi order pada data harga usang (stale quote) atau abnormal (spread spike).
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from loguru import logger

from src.data.market import Quote


@dataclass
class DataValidationResult:
    is_valid: bool
    reason: Optional[str] = None
    quote_age_seconds: float = 0.0


class FreshnessGate:
    """Gatekeeper validasi kesegaran dan kewajaran data market."""

    def __init__(self, max_stale_seconds: float = 5.0, max_spread_pips: float = 3.0):
        self.max_stale_seconds = max_stale_seconds
        self.max_spread_pips = max_spread_pips

    def validate_quote(self, quote: Quote, now_utc: Optional[datetime] = None) -> DataValidationResult:
        now = now_utc or datetime.now(timezone.utc)
        age = (now - quote.timestamp_utc).total_seconds()

        # 1. Cek timestamp masa depan (anomali clock)
        if age < -2.0:
            msg = f"Clock skew anomaly: Quote timestamp is {abs(age):.2f}s in the future"
            logger.warning(f"FreshnessGate: {msg}")
            return DataValidationResult(is_valid=False, reason=msg, quote_age_seconds=age)

        # 2. Cek kesegaran data (Staleness check)
        if age > self.max_stale_seconds:
            msg = f"Stale quote rejected: age {age:.2f}s exceeds limit {self.max_stale_seconds:.2f}s"
            logger.warning(f"FreshnessGate: {msg}")
            return DataValidationResult(is_valid=False, reason=msg, quote_age_seconds=age)

        # 3. Cek kewajaran harga (Sanity check)
        if quote.bid <= 0 or quote.ask <= 0:
            msg = f"Non-positive price: Bid={quote.bid}, Ask={quote.ask}"
            logger.error(f"FreshnessGate: {msg}")
            return DataValidationResult(is_valid=False, reason=msg, quote_age_seconds=age)

        if quote.ask < quote.bid:
            msg = f"Inverted spread: Ask {quote.ask} < Bid {quote.bid}"
            logger.error(f"FreshnessGate: {msg}")
            return DataValidationResult(is_valid=False, reason=msg, quote_age_seconds=age)

        # 4. Cek spread limit
        if quote.spread_pips > self.max_spread_pips:
            msg = f"Spread spike rejected: {quote.spread_pips:.2f} pips exceeds limit {self.max_spread_pips:.2f} pips"
            logger.warning(f"FreshnessGate: {msg}")
            return DataValidationResult(is_valid=False, reason=msg, quote_age_seconds=age)

        return DataValidationResult(is_valid=True, quote_age_seconds=age)
