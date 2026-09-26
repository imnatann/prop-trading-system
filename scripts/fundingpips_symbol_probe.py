"""
FundingPips symbol probe.

    python -m scripts.fundingpips_symbol_probe EURUSD
    python -m scripts.fundingpips_symbol_probe EURUSD --save-profile

Prints broker-native symbol metadata that can be resolved from the terminal instead of
assumed. Read-only; cannot place an order.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from scripts.fundingpips_cli import banner, build_adapter, connect_and_report, field, safe_main
from src.execution.execution_profile import build_profile
from src.execution.symbol_mapper import available_symbol_names, resolve_symbol


def run(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Probe FundingPips symbol metadata (read-only).")
    parser.add_argument("symbol", nargs="?", default="EURUSD", help="Canonical symbol, e.g. EURUSD")
    parser.add_argument("--save-profile", action="store_true",
                        help="Write data/execution/fundingpips/execution_profile.json")
    parser.add_argument("--save-path", default=None, help="Override the profile output path")
    parser.add_argument("--mock", action="store_true", help="Run with simulated MT5 gateway")
    parser.add_argument("--list-matches", type=int, default=0,
                        help="Also print up to N provider symbols matching the canonical root")
    args = parser.parse_args(argv)

    adapter = build_adapter(allow_order=False, mock=True) if args.mock else build_adapter(allow_order=False)
    failure = connect_and_report(adapter)
    if failure is not None:
        return failure

    try:
        info = adapter.symbol_info(args.symbol)
        account = adapter.account_info()
        tick = adapter.tick(args.symbol)

        banner("FundingPips symbol probe: %s" % args.symbol)
        for key, value in info.to_dict().items():
            field(key, value)

        banner("Live quote")
        field("bid", tick.bid)
        field("ask", tick.ask)
        field("mid", tick.mid)
        field("spread_price", tick.spread_price)
        field("spread_pips", (tick.ask - tick.bid) / info.pip_size if info.pip_size else 0.0)
        field("timestamp_utc", tick.timestamp_utc.isoformat())
        field("valid", tick.is_valid)

        banner("JSON-safe representation")
        print(json.dumps(info.to_dict(), indent=2, sort_keys=True))

        if args.list_matches:
            names = available_symbol_names(adapter._require_module()) if adapter._mt5 else []
            root = args.symbol.upper()
            matches = [n for n in names if root in n.upper()]
            banner("Provider symbols containing %r (first %d)" % (root, args.list_matches))
            for name in sorted(matches)[: args.list_matches]:
                print("   ", name)
            if not matches:
                print("    (none)")

        if args.save_profile:
            profile = build_profile(info, account=account, notes="symbol_probe")
            target = profile.save(args.save_path) if args.save_path else profile.save()
            print()
            print("Execution profile saved to: %s" % target)
            print("(This file contains no credentials.)")

        print()
        print("Probe complete. No order was placed.")
        return 0
    finally:
        adapter.disconnect()


if __name__ == "__main__":
    sys.exit(safe_main(run))

