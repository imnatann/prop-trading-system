"""
FundingPips live spread recorder.

    python -m scripts.fundingpips_record_spread --symbol EURUSD --duration 3600

Connects read-only, resolves EURUSD, samples live bid/ask, derives spread in price
points and pips, and persists UTC-stamped JSONL to
data/execution/fundingpips/canonical/.

Guarantees:
  * places NO order: the adapter is built with allow_order=False
  * imports NO strategy, alpha, or research-signal module
  * timestamps are UTC throughout
  * Ctrl+C exits cleanly and leaves a valid, append-complete file
  * persistence uses O_APPEND + fsync, so a crash cannot rewrite earlier observations
  * telemetry is written ONLY under data/execution/, never into a research directory
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from typing import Optional

from scripts.fundingpips_cli import banner, build_adapter, connect_and_report, field, safe_main
from src.execution.telemetry import (
    CANONICAL_DIR,
    RAW_DIR,
    SpreadRecorder,
    exec_event,
)


def run(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Record FundingPips live spread (read-only, no trades).")
    parser.add_argument("--symbol", default="EURUSD", help="Canonical symbol")
    parser.add_argument("--duration", type=float, default=3600.0, help="Seconds to record")
    parser.add_argument("--interval", type=float, default=0.5, help="Seconds between samples")
    parser.add_argument("--output-dir", default=None, help="Override canonical output directory")
    parser.add_argument("--no-save", action="store_true", help="Record but do not persist")
    parser.add_argument("--mock", action="store_true", help="Run with simulated MT5 gateway")
    args = parser.parse_args(argv)

    adapter = build_adapter(allow_order=False, mock=True) if args.mock else build_adapter(allow_order=False)
    failure = connect_and_report(adapter)
    if failure is not None:
        return failure

    recorder = SpreadRecorder(args.symbol)
    started = time.monotonic()
    deadline = started + max(0.0, args.duration)

    exec_event("fundingpips_record_spread", "recording_started", success=True,
               canonical_symbol=args.symbol, duration=args.duration, interval=args.interval)

    try:
        info = adapter.symbol_info(args.symbol)
        banner("FundingPips spread recorder")
        field("Canonical symbol", info.canonical_symbol)
        field("Provider symbol", info.provider_symbol)
        field("Pip size", info.pip_size)
        field("Duration", "%.0fs" % args.duration)
        field("Interval", "%.2fs" % args.interval)
        print()
        print("Recording... press Ctrl+C to stop early.")

        next_sample = time.monotonic()
        while True:
            now = time.monotonic()
            if now >= deadline:
                break
            if now >= next_sample:
                try:
                    tick = adapter.tick(args.symbol)
                    recorder.add_tick(tick, pip_size=info.pip_size, point=info.point)
                except Exception as exc:  # transient quote gaps must not kill the run
                    recorder.rejected += 1
                    exec_event("fundingpips_record_spread", "sample_skipped",
                               success=False, reason=str(exc))
                next_sample = now + max(0.01, args.interval)
            remaining = min(deadline, next_sample) - time.monotonic()
            if remaining > 0:
                time.sleep(min(remaining, 0.25))
    except KeyboardInterrupt:
        print()
        print("Interrupted; finalising recorded samples.")
    finally:
        elapsed = time.monotonic() - started

        stats = recorder.stats()
        banner("Spread summary")
        field("Samples recorded", recorder.count)
        field("Invalid quotes skipped", recorder.rejected)
        field("Elapsed", "%.1fs" % elapsed)
        if stats.samples:
            field("Median (p50)", "%.3f pip" % stats.p50)
            field("p75", "%.3f pip" % stats.p75)
            field("p90", "%.3f pip" % stats.p90)
            field("p95", "%.3f pip" % stats.p95)
            field("p99", "%.3f pip" % stats.p99)
            field("Mean", "%.3f pip" % stats.mean)
            field("Min / Max", "%.3f / %.3f pip" % (stats.minimum, stats.maximum))
            if stats.by_session:
                print()
                print("Median spread by session (UTC):")
                for name, value in sorted(stats.by_session.items()):
                    print("    %-10s %.3f pip" % (name, value))

        if recorder.count and not args.no_save:
            target = recorder.write_jsonl(directory=args.output_dir)
            print()
            print("Telemetry appended to: %s" % target)
            print("Raw un-normalised provider records belong in: %s" % RAW_DIR)

        exec_event("fundingpips_record_spread", "recording_finished", success=True,
                   canonical_symbol=args.symbol, samples=recorder.count,
                   rejected=recorder.rejected)

        adapter.disconnect()
        print()
        print("Recorder complete. No order was placed.")
    return 0


if __name__ == "__main__":
    sys.exit(safe_main(run))

