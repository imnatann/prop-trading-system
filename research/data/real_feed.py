"""Fetch REAL EURUSD daily bars from Yahoo Finance, with honest provenance.

Why daily, not intraday
-----------------------
Measured on 2026-09-25 from this machine:

    interval  range   bars      span
    1d        10y     2611      10.00 years   <-- usable for walk-forward
    1h        2y      12530      2.00 years
    15m       60d      5739      0.22 years   <-- NOT usable
    5m        60d     17215      0.22 years   <-- NOT usable

The binding constraint is the SMALLEST timeframe, exactly as protocol
amendment A01 recorded.  Yahoo will not serve multi-year M15, so the Alpha v2
MTF hypothesis (H4/H1/M15) cannot be tested on this feed.  What CAN be tested
is a daily-horizon hypothesis, and this module exists to make that possible
without pretending the data is something it is not.

What this data is NOT
---------------------
Yahoo returns INDICATIVE OHLC, not executable bid/ask quotes:
  * no bid/ask, so the spread must be MODELLED (protocol A05 already says so)
  * no tick data, so intrabar breach detection can only use bar extremes
  * daily closes are a single venue's snapshot, not a consolidated print
Every one of those limitations must travel with any result derived from it.

The module writes a JSON provenance sidecar next to the CSV so a result can
never be separated from the feed that produced it.
"""
from __future__ import annotations

import csv
import datetime as _dt
import json
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

#: Yahoo FX symbols are suffixed with "=X".
YAHOO_SYMBOLS: Dict[str, str] = {
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "AUDUSD": "AUDUSD=X",
    "USDJPY": "JPY=X",
    "USDCAD": "CAD=X",
    "USDCHF": "CHF=X",
    "XAUUSD": "GC=F",
    "BTCUSD": "BTC-USD",
    "BTC": "BTC-USD",
}

from src.paths import REAL_DATA_DIR as OUT_DIR

INTERVAL_LIMITS = {
    "1d": "10y is reliable (2611 bars measured)",
    "1h": "2y maximum (12530 bars measured)",
    "15m": "60 days only - NOT sufficient for walk-forward",
    "5m": "60 days only - NOT sufficient for walk-forward",
}


#: Pairs quoted to 3 decimals (JPY crosses). A "pip" for these is 0.01, not
#: 0.0001. Hardcoding 0.0001 makes a 60-pip stop 0.006 JPY on a 157.00 quote --
#: a stop 0.004% away, which is not a trade anyone could place. Getting this
#: wrong does not raise an error; it silently reports results for a strategy
#: nobody ran.
JPY_QUOTED = ("USDJPY", "EURJPY", "GBPJPY", "AUDJPY", "CADJPY", "CHFJPY",
              "NZDJPY")

#: Pip size by quote convention.
PIP_STANDARD = 0.0001
PIP_JPY = 0.01


def pip_size_for(symbol: str) -> float:
    """Pip size for a symbol, from its QUOTE convention.

    JPY pairs quote to 3 decimals, so one pip is 0.01. Everything else is the
    usual 0.0001. This is the single place the convention is decided, so a
    caller cannot get it right in one module and wrong in another.
    """
    if "_" in symbol:
        # tolerate OANDA-style names such as USD_JPY
        symbol = symbol.replace("_", "")
    return PIP_JPY if symbol.upper() in JPY_QUOTED else PIP_STANDARD


def pip_value_per_lot_for(symbol: str, contract_size: float = 100_000.0,
                          jpy_rate: Optional[float] = None) -> float:
    """Account-currency (USD) value of one pip for one standard lot.

    For a USD-QUOTED pair the arithmetic already lands in USD:
        EURUSD: 0.0001 x 100,000 = 10.00 USD per pip        -> correct as-is

    For a JPY-QUOTED pair it lands in JPY, NOT USD:
        USDJPY: 0.01 x 100,000 = 1,000 JPY per pip
    Dividing by the rate (1,000 / 148.8 = 6.72 USD) is REQUIRED. Skipping it
    overstated the position value by ~149x, which made balances jump from
    10,000 to 22,000 in four trades and breached almost every fold instantly.

    Passing jpy_rate=None for a JPY pair raises rather than silently defaulting
    to 1.0, because a silent 1.0 is exactly the bug being fixed here.
    """
    key = symbol.upper().replace("_", "")
    per_unit = pip_size_for(symbol) * contract_size
    if key in JPY_QUOTED:
        if jpy_rate is None or jpy_rate <= 0:
            raise ValueError(
                "pip value for %s needs the USDJPY rate to convert JPY into the "
                "account currency; got jpy_rate=%r" % (symbol, jpy_rate))
        return per_unit / jpy_rate
    return per_unit


def usd_pip_value_for_bar(symbol: str, rate: float,
                          contract_size: float = 100_000.0) -> float:
    """Pip value in USD for a given USDJPY rate. Convenience for per-bar use."""
    return pip_value_per_lot_for(symbol, contract_size=contract_size,
                                 jpy_rate=rate)


@dataclass(frozen=True)
class Candle:
    timestamp: _dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


def _http_json(url: str, timeout: float = 30.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_yahoo(symbol: str, interval: str = "1d",
                range_: str = "10y") -> Tuple[List[Candle], dict]:
    """Download OHLC bars. Returns (candles, metadata)."""
    ysym = YAHOO_SYMBOLS.get(symbol.upper(), symbol)
    url = YAHOO_CHART.format(symbol=urllib.parse.quote(ysym))
    url += "?range=%s&interval=%s" % (range_, interval)
    payload = _http_json(url)

    chart = payload.get("chart") or {}
    if chart.get("error"):
        raise RuntimeError("yahoo error: %s" % chart["error"])
    results = chart.get("result") or []
    if not results:
        raise RuntimeError("yahoo returned no result for %s" % symbol)
    res = results[0]

    stamps = res.get("timestamp") or []
    quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    vols = quote.get("volume") or []

    out: List[Candle] = []
    dropped = 0
    for i, ts in enumerate(stamps):
        try:
            o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        except IndexError:
            dropped += 1
            continue
        if None in (o, h, l, c):
            dropped += 1          # Yahoo emits nulls for holidays/partial bars
            continue
        out.append(Candle(
            timestamp=_dt.datetime.fromtimestamp(int(ts), _dt.timezone.utc),
            open=float(o), high=float(h), low=float(l), close=float(c),
            volume=float(vols[i]) if i < len(vols) and vols[i] is not None else 0.0,
        ))

    meta = {
        "symbol": symbol.upper(),
        "yahoo_symbol": ysym,
        "interval": interval,
        "range": range_,
        "bars": len(out),
        "dropped_null_bars": dropped,
        "first_utc": out[0].timestamp.isoformat() if out else None,
        "last_utc": out[-1].timestamp.isoformat() if out else None,
        "retrieved_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "source": "Yahoo Finance chart API (indicative OHLC, NOT executable quotes)",
        "limitations": [
            "no bid/ask: spread is MODELLED, not observed",
            "no tick data: intrabar path limited to bar extremes",
            "not a consolidated tape",
        ],
    }
    return out, meta


def quality_report(candles: Sequence[Candle]) -> dict:
    """Structural checks. A gap or a crossed bar invalidates downstream work."""
    if not candles:
        return {"bars": 0, "ok": False, "problems": ["no data"]}

    problems: List[str] = []
    crossed = zero_range = nonpositive = 0
    prev_ts = None
    gaps: List[int] = []
    max_gap_days = 0.0

    for c in candles:
        if c.high < c.low:
            crossed += 1
        if c.high == c.low:
            zero_range += 1
        if min(c.open, c.high, c.low, c.close) <= 0:
            nonpositive += 1
        if prev_ts is not None:
            delta = (c.timestamp - prev_ts).days
            # FX trades ~5 days/week; a 3+ day gap is a holiday run, not an error
            if delta > 4:
                gaps.append(delta)
                max_gap_days = max(max_gap_days, float(delta))
        prev_ts = c.timestamp

    if crossed:
        problems.append("%d crossed bars (high < low)" % crossed)
    if nonpositive:
        problems.append("%d non-positive prices" % nonpositive)

    span_days = (candles[-1].timestamp - candles[0].timestamp).days
    return {
        "bars": len(candles),
        "first_utc": candles[0].timestamp.isoformat(),
        "last_utc": candles[-1].timestamp.isoformat(),
        "span_years": round(span_days / 365.25, 3),
        "crossed_bars": crossed,
        "zero_range_bars": zero_range,
        "gaps_over_4_days": len(gaps),
        "max_gap_days": max_gap_days,
        "problems": problems,
        "ok": not problems,
    }


def save_csv(candles: Sequence[Candle], symbol: str, interval: str,
             out_dir: Optional[Path] = None) -> Path:
    d = Path(out_dir or OUT_DIR)
    d.mkdir(parents=True, exist_ok=True)
    path = d / ("%s_%s.csv" % (symbol.upper(), interval))
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["timestamp_utc", "open", "high", "low", "close", "volume"])
        for c in candles:
            w.writerow([c.timestamp.isoformat(), c.open, c.high, c.low,
                        c.close, c.volume])
    return path


def save_provenance(meta: dict, quality: dict, symbol: str, interval: str,
                    out_dir: Optional[Path] = None) -> Path:
    """Write the sidecar so a result can never be separated from its feed."""
    d = Path(out_dir or OUT_DIR)
    d.mkdir(parents=True, exist_ok=True)
    path = d / ("%s_%s.provenance.json" % (symbol.upper(), interval))
    payload = {"meta": meta, "quality": quality,
               "interval_limits": INTERVAL_LIMITS}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def load_csv(path) -> List[Candle]:
    """Read back a saved CSV into Candle objects."""
    out: List[Candle] = []
    with Path(path).open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            out.append(Candle(
                timestamp=_dt.datetime.fromisoformat(row["timestamp_utc"]),
                open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]),
                volume=float(row.get("volume") or 0.0),
            ))
    return out


import urllib.parse  # noqa: E402  (used by fetch_yahoo)
