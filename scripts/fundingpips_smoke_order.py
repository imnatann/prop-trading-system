"""
FundingPips smoke-order utility.

    python -m scripts.fundingpips_smoke_order --symbol EURUSD --volume 0.01 --allow-order

TWO INDEPENDENT OPT-INS ARE REQUIRED before a single order can be sent:
    1. FUNDINGPIPS_ALLOW_TRADING=true   (configuration)
    2. --allow-order                    (this explicit command flag)
Neither alone is sufficient, and nothing in this repository enables trading as a
side effect of importing or configuring anything.

Without --allow-order the tool performs a full PREFLIGHT DRY RUN: it connects, runs
every precondition check, and prints exactly what it would send - but sends nothing.

Purpose: validate execution plumbing and accounting, NOT profit. The sequence is a
single BUY then a single close (or a single SELL then a single close). It never loops,
never martingales, and never retries a rejected order with a larger volume.

Anti-scalping guard: FundingPips' terms treat sub-second round trips as prohibited
high-frequency activity, so the default hold is 60 seconds, taken from
PropFirmRules.min_trade_duration_seconds rather than invented here.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Optional

from config.prop_rules import FUNDING_PIPS_2STEP
from scripts.fundingpips_cli import banner, build_adapter, connect_and_report, field, safe_main
from src.execution.errors import OrderPreflightError, TradingDisabledError
from src.execution.models import OrderIntent, OrderSide
from src.execution.readiness import (
    build_evidence_from_profile,
    evaluate_readiness,
    load_readiness,
    save_readiness,
)
from src.execution.telemetry import attribute_round_trip, exec_event
from src.execution.trade_journal import TradeJournal, get_trade_journal

#: The smoke order is always a single minimum-size ticket unless the broker's own
#: minimum forces something larger.
SMOKE_VOLUME = 0.01


def _print_preflight_failure(exc: OrderPreflightError) -> None:
    print()
    print("PREFLIGHT REJECTED")
    print("-" * 17)
    print("  reason_code: %s" % exc.reason_code)
    print("  detail     : %s" % str(exc))
    for key, value in getattr(exc, "context", {}).items():
        print("  %s = %s" % (key, value))


def run(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="FundingPips smoke order (requires two independent opt-ins)."
    )
    parser.add_argument("--symbol", default="EURUSD")
    parser.add_argument("--volume", type=float, default=SMOKE_VOLUME)
    parser.add_argument("--side", choices=["BUY", "SELL"], default="BUY")
    parser.add_argument("--allow-order", action="store_true",
                        help="Explicit acknowledgement required IN ADDITION to "
                             "FUNDINGPIPS_ALLOW_TRADING=true")
    parser.add_argument("--max-spread-pips", type=float, default=3.0,
                        help="Spread safety ceiling; preflight rejects above this")
    parser.add_argument("--hold-seconds", type=float, default=float(FUNDING_PIPS_2STEP.min_trade_duration_seconds),
                        help="Minimum seconds to hold before closing, to respect the "
                             "anti-scalping rule")
    parser.add_argument("--close", dest="close", action="store_true", default=True,
                        help="Close the position after the hold (default)")
    parser.add_argument("--keep-open", dest="close", action="store_false",
                        help="Leave the position open instead of closing it")
    parser.add_argument("--allow-trading-flag-ack", action="store_true",
                        help="Acknowledge that FUNDINGPIPS_ALLOW_TRADING will be set")
    parser.add_argument("--skip-readiness-check", action="store_true",
                        help="Send despite missing stage-1 evidence. ONLY for a "
                             "deliberate, recorded exception: the evidence gate "
                             "exists because an unmeasured spread makes the cost "
                             "attribution meaningless.")
    parser.add_argument("--mock", action="store_true",
                        help="Run with simulated MT5 gateway (allows full lifecycle test on macOS/Linux)")
    parser.add_argument("--readiness-path", default=None,
                        help="Override where the stage-1 readiness REPORT is "
                             "written. Defaults to the standard evidence dir.")
    parser.add_argument("--evidence-dir", default=None,
                        help="Override the evidence directory used to BUILD the "
                             "report. Useful for tests and for isolated runs.")
    parser.add_argument("--trades-dir", default=None,
                        help="Override directory where trade ledger (JSONL and CSV) are saved. "
                             "Defaults to data/trades/")
    args = parser.parse_args(argv)

    trades_dir_override = Path(args.trades_dir) if args.trades_dir else (Path(args.evidence_dir) / "trades" if args.evidence_dir else None)
    journal = TradeJournal(trades_dir_override)

    adapter = build_adapter(allow_order=bool(args.allow_order),
                            max_spread_pips=args.max_spread_pips,
                            mock=True) if args.mock else build_adapter(allow_order=bool(args.allow_order),
                            max_spread_pips=args.max_spread_pips)
    cfg = adapter.config

    banner("FundingPips smoke order")
    field("Symbol", args.symbol)
    field("Side", args.side)
    field("Volume", args.volume)
    field("Config trading flag", "true" if cfg.allow_trading else "false")
    field("Explicit --allow-order", "yes" if args.allow_order else "no")
    field("Mode", "LIVE ORDER" if (cfg.allow_trading and args.allow_order) else "PREFLIGHT DRY RUN")

    # Report the two gates explicitly before touching the network.
    if not cfg.allow_trading or not args.allow_order:
        missing = []
        if not cfg.allow_trading:
            missing.append("FUNDINGPIPS_ALLOW_TRADING=true")
        if not args.allow_order:
            missing.append("--allow-order")
        print()
        print("BOTH opt-ins are required. Missing: %s" % ", ".join(missing))
        print("Proceeding in PREFLIGHT DRY RUN mode: no order will be sent.")

    failure = connect_and_report(adapter)
    if failure is not None:
        return failure

    try:
        info = adapter.symbol_info(args.symbol)
        account = adapter.account_info()
        tick = adapter.tick(args.symbol)

        banner("Account and market")
        field("Server", account.server)
        field("Account", account.masked_login)
        field("Account trading enabled", "YES" if account.trade_allowed else "NO")
        field("Provider symbol", info.provider_symbol)
        field("Trade mode", info.trade_mode.value)
        field("Bid / Ask", "%.5f / %.5f" % (tick.bid, tick.ask))
        spread_pips = (tick.ask - tick.bid) / info.pip_size if info.pip_size else 0.0
        field("Spread", "%.2f pip (ceiling %.2f)" % (spread_pips, args.max_spread_pips))
        field("Market open", "YES" if tick.is_valid else "NO / invalid quote")
        field("Min volume", info.volume_min)
        field("Volume step", info.volume_step)

        # Volume sanity, reported before preflight so the operator sees the reasoning.
        volume = float(args.volume)
        if info.volume_min > 0 and volume < info.volume_min:
            print()
            print("REFUSING: volume %.4f is below broker minimum %.4f. The volume is NOT"
                  " silently raised; adjust it explicitly if that is intended."
                  % (volume, info.volume_min))
            return 0
        if info.volume_step > 0:
            steps = volume / info.volume_step
            if abs(steps - round(steps)) > 1e-6:
                print()
                print("REFUSING: volume %.4f is not aligned to volume_step %.4f. The volume"
                      " is NOT silently repaired." % (volume, info.volume_step))
                return 0

        intent = OrderIntent(
            symbol=args.symbol,
            side=OrderSide.BUY if args.side == "BUY" else OrderSide.SELL,
            volume=volume,
            sl=0.0,
            tp=0.0,
            client_order_id="QP-SMOKE-%d" % int(time.time()),
            comment="QP smoke",
        )

        # ---- full preflight. Runs in BOTH modes so dry runs are meaningful.
        if not (cfg.allow_trading and args.allow_order):
            # preflight itself enforces the gates; report the resulting reason clearly
            try:
                adapter.preflight(intent, equity=account.equity, free_margin=account.margin_free)
            except OrderPreflightError as exc:
                _print_preflight_failure(exc)
                banner("Dry run result")
                print("All channel checks that do not require write permission passed.")
                print("Order NOT sent. Set both opt-ins to execute.")
                return 0
            print()
            print("Preflight passed. Order would be:")
            print("    %s %.2f %s (%s)" % (args.side, volume, info.provider_symbol, intent.client_order_id))
            print("Order NOT sent (dry run).")
            return 0

        try:
            adapter.preflight(intent, equity=account.equity, free_margin=account.margin_free)
        except OrderPreflightError as exc:
            _print_preflight_failure(exc)
            return 4

        # ------------------------------------------------- stage-1 readiness
        # THIRD gate, and the only evidence-based one. The two opt-ins prove
        # intent; this proves the venue is understood. An unmeasured spread
        # makes the cost attribution this tool prints afterwards meaningless,
        # so it blocks rather than warns.
        # The evidence DIRECTORY and the report PATH are different things and
        # were previously conflated: passing the profile path as the readiness
        # path meant a run with no override wrote to the real data directory.
        _ev_dir = Path(args.evidence_dir) if args.evidence_dir else None
        _conn_path = (_ev_dir / "connection_evidence.json") if _ev_dir else None
        _spread_dir = (_ev_dir / "canonical") if _ev_dir else None
        _profile_path = ((_ev_dir / "execution_profile.json") if _ev_dir
                         else (Path(args.readiness_path) if args.readiness_path
                               else None))
        _report_path = Path(args.readiness_path) if args.readiness_path else ((_ev_dir / "stage1_readiness.json") if _ev_dir else None)

        _evidence = build_evidence_from_profile(
            profile_path=_profile_path,
            connection_path=_conn_path,
            spread_dir=_spread_dir,
            connection={
                "logged_in": True,
                "server_matches_expected": bool(getattr(cfg, "server_is_expected", True)),
                "account_number_present": bool(getattr(account, "login", 0)),
            },
            config={"allow_trading": bool(cfg.allow_trading)},
        )
        _report = evaluate_readiness(_evidence)
        banner("Stage-1 readiness gate")
        print(_report.render())
        save_readiness(_report, _report_path)
        if not _report.ready:
            if not args.skip_readiness_check:
                print()
                print("REFUSING TO SEND: stage-1 evidence is incomplete.")
                print("Run, on the Windows host, in this order:")
                print("    python -m scripts.fundingpips_connection_check")
                print("    python -m scripts.fundingpips_symbol_probe EURUSD --save-profile")
                print("    python -m scripts.fundingpips_record_spread --symbol EURUSD --duration 3600")
                print("Then re-run this command. Nothing was sent.")
                return 4
            print()
            print("WARNING: --skip-readiness-check was supplied. Proceeding WITHOUT")
            print("full evidence. Any cost attribution printed below is unreliable.")

        # ---------------------------------------------------------- real order
        banner("Submitting ONE smoke order")
        exec_event("fundingpips_smoke_order", "smoke_submit", success=None,
                   side=args.side, volume=volume, symbol=args.symbol)

        result = adapter.place_order(intent)
        field("Success", result.success)
        field("Order id", result.order_id)
        field("Requested price", result.requested_price)
        field("Filled price", result.filled_price)
        field("Slippage", "%.5f price / %.2f pip"
              % (result.slippage_price, result.slippage_pips(info.pip_size)))
        field("Status", result.status.value)
        field("Retcode", "%s (%s)" % (result.retcode, result.retcode_name))

        mode_str = "MOCK" if args.mock else ("DEMO" if getattr(account, "is_demo", True) else "LIVE")
        journal.record_open(
            trade_id=str(result.order_id or intent.client_order_id or "smoke-1"),
            symbol=args.symbol,
            side=args.side,
            volume=volume,
            requested_price=result.requested_price,
            filled_price=result.filled_price,
            slippage_pips=result.slippage_pips(info.pip_size) if result.success else 0.0,
            status=result.status.value,
            retcode=result.retcode,
            mode=mode_str,
            comment=intent.comment or "QP-SMOKE",
            account=account.masked_login,
            server=account.server,
        )

        if not result.success:
            print()
            print("Order REJECTED. Not retrying, and not increasing volume. Resolve the")
            print("broker-side reason before trying again.")
            return 4

        # ---------------------------------------------------- hold then close
        if args.close and args.hold_seconds > 0:
            print()
            print("Holding %.0fs to respect the anti-scalping minimum-hold rule..."
                  % args.hold_seconds)
            time.sleep(args.hold_seconds)

        if args.close:
            open_positions = adapter.positions(args.symbol)
            if not open_positions:
                print()
                print("No open position found to close; it may have hit SL/TP.")
                return 0

            position = open_positions[0]
            banner("Closing the smoke position")
            close_result = adapter.close_position(position.position_id, reason="QP smoke close")
            field("Success", close_result.success)
            field("Filled price", close_result.filled_price)
            field("Retcode", "%s (%s)" % (close_result.retcode, close_result.retcode_name))

            entry_price = position.entry_price
            entry_mid = (tick.bid + tick.ask) / 2.0
            exit_price = close_result.filled_price
            exit_tick = adapter.tick(args.symbol)
            exit_mid = (exit_tick.bid + exit_tick.ask) / 2.0

            if exit_price and entry_price:
                breakdown = attribute_round_trip(
                    symbol=args.symbol,
                    side=args.side,
                    volume=position.volume,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    entry_mid=entry_mid,
                    exit_mid=exit_mid,
                    pip_size=info.pip_size,
                    contract_size=info.contract_size,
                    commission=position.commission,
                    financing=position.swap,
                )
                banner("Cost attribution (single-counted)")
                for key, value in breakdown.to_dict().items():
                    field(key, value)

                journal.record_close(
                    trade_id=str(position.position_id),
                    symbol=args.symbol,
                    side=args.side,
                    volume=position.volume,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    gross_pnl=breakdown.gross_pnl,
                    net_pnl=breakdown.net_pnl,
                    commission=breakdown.commission,
                    swap=breakdown.financing,
                    hold_duration_sec=args.hold_seconds,
                    status="CLOSED" if close_result.success else "CLOSE_FAILED",
                    retcode=close_result.retcode,
                    mode=mode_str,
                    comment="QP smoke close",
                    attribution=breakdown.to_dict(),
                )

        print()
        banner("Trade Journal Recorded")
        field("Directory", journal.directory)
        field("JSONL Ledger", journal.jsonl_path)
        field("CSV Summary", journal.csv_path)

        print()
        print("Smoke sequence complete. Single order only; no loop, no martingale.")
        return 0
    finally:
        adapter.disconnect()


if __name__ == "__main__":
    sys.exit(safe_main(run))

