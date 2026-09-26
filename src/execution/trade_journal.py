"""
Dedicated Trade Journal / Execution Recorder.

Mencatat seluruh eksekusi trade secara permanen dan terstruktur:
  * Mulai dari tahap testing & smoke order (--mock atau live)
  * Hingga tahap production ready.

Lokasi penyimpanan default:
  data/trades/
    ├── trades.jsonl  (Append-only, lengkap dengan metadata & cost attribution)
    └── trades.csv    (Format tabel CSV yang langsung bisa dibuka di Excel/Numbers)

Setiap trade di-flush seketika ke disk (os.fsync) sehingga tidak akan hilang
meskipun terminal ditutup atau proses mati mendadak.
"""
from __future__ import annotations

import csv
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.paths import TRADES_CSV_PATH, TRADES_DIR, TRADES_LEDGER_PATH

CSV_FIELDNAMES = [
    "timestamp_utc",
    "trade_id",
    "action",
    "mode",
    "symbol",
    "side",
    "volume",
    "requested_price",
    "filled_price",
    "slippage_pips",
    "exit_price",
    "gross_pnl",
    "net_pnl",
    "commission",
    "swap",
    "hold_duration_sec",
    "status",
    "retcode",
    "comment",
]


class TradeJournal:
    """Thread-safe, crash-resilient trade recorder."""

    def __init__(self, directory: Optional[Path] = None):
        self.directory = Path(directory) if directory else TRADES_DIR
        self.jsonl_path = self.directory / "trades.jsonl"
        self.csv_path = self.directory / "trades.csv"
        self._lock = threading.Lock()

    def _ensure_directory(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        if not self.csv_path.exists():
            with self.csv_path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
                writer.writeheader()
                f.flush()
                os.fsync(f.fileno())

    def record_open(
        self,
        *,
        trade_id: str,
        symbol: str,
        side: str,
        volume: float,
        requested_price: float,
        filled_price: float,
        slippage_pips: float = 0.0,
        sl: float = 0.0,
        tp: float = 0.0,
        status: str = "OPEN",
        retcode: int = 10009,
        mode: str = "LIVE",
        comment: str = "",
        account: str = "",
        server: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Record an opened trade / fill event."""
        now_utc = datetime.now(timezone.utc).isoformat()
        entry = {
            "timestamp_utc": now_utc,
            "trade_id": str(trade_id),
            "action": "OPEN",
            "mode": str(mode).upper(),
            "symbol": str(symbol),
            "side": str(side).upper(),
            "volume": float(volume),
            "requested_price": float(requested_price),
            "filled_price": float(filled_price),
            "slippage_pips": round(float(slippage_pips), 3),
            "sl": float(sl),
            "tp": float(tp),
            "status": str(status),
            "retcode": int(retcode),
            "comment": str(comment),
            "account": str(account),
            "server": str(server),
        }
        if extra:
            entry["extra"] = extra

        self._write_entry(entry)
        return entry

    def record_close(
        self,
        *,
        trade_id: str,
        symbol: str,
        side: str,
        volume: float,
        entry_price: float,
        exit_price: float,
        gross_pnl: float = 0.0,
        net_pnl: float = 0.0,
        commission: float = 0.0,
        swap: float = 0.0,
        hold_duration_sec: float = 0.0,
        status: str = "CLOSED",
        retcode: int = 10009,
        mode: str = "LIVE",
        comment: str = "",
        attribution: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Record a closed trade and its round-trip cost attribution."""
        now_utc = datetime.now(timezone.utc).isoformat()
        entry = {
            "timestamp_utc": now_utc,
            "trade_id": str(trade_id),
            "action": "CLOSE",
            "mode": str(mode).upper(),
            "symbol": str(symbol),
            "side": str(side).upper(),
            "volume": float(volume),
            "entry_price": float(entry_price),
            "exit_price": float(exit_price),
            "gross_pnl": round(float(gross_pnl), 2),
            "net_pnl": round(float(net_pnl), 2),
            "commission": round(float(commission), 2),
            "swap": round(float(swap), 2),
            "hold_duration_sec": round(float(hold_duration_sec), 2),
            "status": str(status),
            "retcode": int(retcode),
            "comment": str(comment),
        }
        if attribution:
            entry["attribution"] = attribution

        self._write_entry(entry)
        return entry

    def _write_entry(self, entry: Dict[str, Any]) -> None:
        with self._lock:
            self._ensure_directory()

            # 1. Append to JSONL
            with self.jsonl_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
                f.flush()
                os.fsync(f.fileno())

            # 2. Append to CSV
            csv_row = {
                "timestamp_utc": entry.get("timestamp_utc", ""),
                "trade_id": entry.get("trade_id", ""),
                "action": entry.get("action", ""),
                "mode": entry.get("mode", ""),
                "symbol": entry.get("symbol", ""),
                "side": entry.get("side", ""),
                "volume": entry.get("volume", ""),
                "requested_price": entry.get("requested_price", ""),
                "filled_price": entry.get("filled_price", ""),
                "slippage_pips": entry.get("slippage_pips", ""),
                "exit_price": entry.get("exit_price", ""),
                "gross_pnl": entry.get("gross_pnl", ""),
                "net_pnl": entry.get("net_pnl", ""),
                "commission": entry.get("commission", ""),
                "swap": entry.get("swap", ""),
                "hold_duration_sec": entry.get("hold_duration_sec", ""),
                "status": entry.get("status", ""),
                "retcode": entry.get("retcode", ""),
                "comment": entry.get("comment", ""),
            }
            with self.csv_path.open("a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
                writer.writerow(csv_row)
                f.flush()
                os.fsync(f.fileno())

    def list_trades(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Read recorded trades from JSONL, newest first."""
        if not self.jsonl_path.exists():
            return []
        trades = []
        with self.jsonl_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        trades.append(json.loads(line))
                    except Exception:
                        continue
        trades.reverse()
        return trades[:limit]


_DEFAULT_JOURNAL: Optional[TradeJournal] = None


def get_trade_journal(directory: Optional[Path] = None) -> TradeJournal:
    """Get or create the singleton TradeJournal instance."""
    global _DEFAULT_JOURNAL
    if directory:
        return TradeJournal(directory)
    if _DEFAULT_JOURNAL is None:
        _DEFAULT_JOURNAL = TradeJournal()
    return _DEFAULT_JOURNAL
