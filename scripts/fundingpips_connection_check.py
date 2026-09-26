"""
FundingPips MT5 read-only connection check.

    python -m scripts.fundingpips_connection_check

Prints connection/account/symbol metadata with the account number masked. This tool
CANNOT place an order: it never passes allow_order=True, so the adapter's write gate
stays closed regardless of the environment.
"""
from __future__ import annotations

import sys

from scripts.fundingpips_cli import (
    banner,
    build_adapter,
    connect_and_report,
    field,
    safe_main,
)
from src.execution.readiness import save_connection_evidence
from src.execution.redaction import MASK


def run(argv: Optional[list] = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="FundingPips MT5 connection check (read-only).")
    parser.add_argument("--mock", action="store_true",
                        help="Run with simulated MT5 gateway (for testing on macOS/Linux)")
    parser.add_argument("--evidence-path", default=None,
                        help="Path to save connection evidence JSON (defaults to standard path)")
    # When called as a function with no arguments (e.g. from tests or safe_main),
    # default to empty args rather than leaking sys.argv from pytest.
    args = parser.parse_args(argv if argv is not None else ([] if "pytest" in sys.modules else sys.argv[1:]))

    adapter = build_adapter(allow_order=False, mock=True) if args.mock else build_adapter(allow_order=False)
    cfg = adapter.config

    failure = connect_and_report(adapter)
    if failure is not None:
        return failure

    try:
        account = adapter.account_info()
        symbol_info = adapter.symbol_info(cfg.symbol)
        tick = adapter.tick(cfg.symbol)

        banner("FundingPips MT5 connection")
        field("Connected", "YES")
        field("Server", account.server)
        field("Account", account.masked_login)
        field("Company", account.company or "(not reported)")
        field("Currency", account.currency)
        field("Leverage", "1:%d" % account.leverage if account.leverage else "(not reported)")
        field("Balance", "%.2f" % account.balance)
        field("Equity", "%.2f" % account.equity)
        field("Free margin", "%.2f" % account.margin_free)
        field("Trading enabled", "YES" if account.trade_allowed else "NO")
        field("Adapter trading", "ENABLED" if adapter.trading_enabled else "DISABLED (safe default)")
        field("Can place orders", "YES" if adapter.can_trade else "NO (two gates required)")

        banner("EURUSD symbol")
        field("Canonical symbol", symbol_info.canonical_symbol)
        field("Provider symbol", symbol_info.provider_symbol)
        field("Digits", symbol_info.digits)
        field("Point", symbol_info.point)
        field("Pip size", symbol_info.pip_size)
        field("Trade mode", symbol_info.trade_mode.value)
        field("Contract size", symbol_info.contract_size)
        field("Min volume", symbol_info.volume_min)
        field("Max volume", symbol_info.volume_max)
        field("Volume step", symbol_info.volume_step)
        field("Stops level", symbol_info.stops_level)
        field("Swap long", symbol_info.swap_long)
        field("Swap short", symbol_info.swap_short)

        banner("Live quote")
        if tick.is_valid:
            field("Bid", "%.*f" % (symbol_info.digits, tick.bid))
            field("Ask", "%.*f" % (symbol_info.digits, tick.ask))
            field("Spread", "%.2f pip" % ((tick.ask - tick.bid) / symbol_info.pip_size))
            field("Timestamp UTC", tick.timestamp_utc.isoformat())
        else:
            field("Quote", "INVALID (crossed or zero): bid=%s ask=%s" % (tick.bid, tick.ask))

        positions = adapter.positions(cfg.symbol)
        orders = adapter.orders(cfg.symbol)
        field("Open positions", len(positions))
        field("Working orders", len(orders))

        # Persist the FACT of this login so the readiness gate can see it.
        # Without this the gate's connection criteria can never pass, which
        # makes it a permanent refusal dressed up as a check. Only the masked
        # account and the server names are written; never the password.
        evidence_path = save_connection_evidence(
            login=int(getattr(account, "login", 0) or 0),
            server=str(account.server),
            expected_server=str(cfg.server),
            path=args.evidence_path,
        )
        print()
        field("Evidence saved", evidence_path)
        field("Login recorded", account.masked_login)
        print()
        print("Read-only check complete. No order was placed.")
        return 0
    finally:
        adapter.disconnect()


if __name__ == "__main__":
    sys.exit(safe_main(run))

