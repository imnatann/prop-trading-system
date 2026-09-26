"""Real market data ingestion with provenance."""
from research.data.real_feed import (
    Candle,
    INTERVAL_LIMITS,
    YAHOO_SYMBOLS,
    fetch_yahoo,
    load_csv,
    quality_report,
    save_csv,
    save_provenance,
)

__all__ = ["Candle", "INTERVAL_LIMITS", "YAHOO_SYMBOLS", "fetch_yahoo",
           "load_csv", "quality_report", "save_csv", "save_provenance"]
