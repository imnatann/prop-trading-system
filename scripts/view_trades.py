"""
View recorded trades from the dedicated trade journal (data/trades/).

    python -m scripts.view_trades
    python -m scripts.view_trades --limit 20
    python -m scripts.view_trades --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from scripts.fundingpips_cli import banner, field, safe_main
from src.execution.trade_journal import get_trade_journal


def run(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="View trades recorded in data/trades/.")
    parser.add_argument("--trades-dir", default=None, help="Override trades directory")
    parser.add_argument("--limit", type=int, default=30, help="Number of recent trades to show")
    parser.add_argument("--json", action="store_true", help="Print output in JSON format")
    args = parser.parse_args(argv)

    journal = get_trade_journal(Path(args.trades_dir) if args.trades_dir else None)
    trades = journal.list_trades(limit=args.limit)

    if args.json:
        print(json.dumps(trades, indent=2))
        return 0

    banner("Trade Execution Journal: %s" % journal.directory)
    field("JSONL Ledger", journal.jsonl_path)
    field("CSV Summary", journal.csv_path)
    field("Total records found", len(trades))
    print()

    if not trades:
        print("No trades recorded yet in %s" % journal.directory)
        return 0

    header = "%-20s | %-8s | %-5s | %-5s | %-7s | %-4s | %-5s | %-8s | %-8s | %-9s | %-7s" % (
        "Timestamp (UTC)", "ID", "Act", "Mode", "Sym", "Side", "Vol", "Entry", "Exit", "Net PnL", "Status"
    )
    print(header)
    print("-" * len(header))

    for t in trades:
        ts = t.get("timestamp_utc", "")[:19].replace("T", " ")
        tid = str(t.get("trade_id", ""))[:8]
        act = str(t.get("action", ""))[:5]
        mode = str(t.get("mode", ""))[:5]
        sym = str(t.get("symbol", ""))[:7]
        side = str(t.get("side", ""))[:4]
        vol = "%.2f" % float(t.get("volume", 0))
        entry = ("%.5f" % float(t.get("filled_price", t.get("entry_price", 0)))) if (t.get("filled_price") or t.get("entry_price")) else "-"
        exit_p = ("%.5f" % float(t.get("exit_price"))) if t.get("exit_price") is not None else "-"
        net_pnl = ("$%.2f" % float(t.get("net_pnl"))) if t.get("net_pnl") is not None else "-"
        status = str(t.get("status", ""))[:7]

        print("%-20s | %-8s | %-5s | %-5s | %-7s | %-4s | %-5s | %-8s | %-8s | %-9s | %-7s" % (
            ts, tid, act, mode, sym, side, vol, entry, exit_p, net_pnl, status
        ))

    print()
    return 0


if __name__ == "__main__":
    sys.exit(safe_main(run))
