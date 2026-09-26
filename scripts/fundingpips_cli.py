"""
Shared helpers for the FundingPips operator CLIs.

Keeps credential handling, redaction, and failure reporting in one place so the four
CLIs cannot drift apart. Nothing here can place an order; the write paths live only
in fundingpips_smoke_order.py and are gated by two independent opt-ins.
"""
from __future__ import annotations

import sys
from typing import Any, Optional

from src.execution.errors import ExecutionError, MissingCredentialError
from src.execution.redaction import redact

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_UNSUPPORTED_ENV = 2
EXIT_MISSING_CREDENTIALS = 3
EXIT_PRECHECK_FAILED = 4


def banner(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


def field(label: str, value: Any) -> None:
    print("%-20s%s" % (label + ":", value))


def report_failure(exc: BaseException) -> int:
    """Print a redacted, actionable failure and return the right exit code."""
    from src.execution.errors import UnsupportedExecutionEnvironmentError

    name = type(exc).__name__
    message = redact(exc)

    if isinstance(exc, UnsupportedExecutionEnvironmentError):
        print()
        print("UNSUPPORTED ENVIRONMENT")
        print("-" * 22)
        print(message)
        print()
        print("This is expected on macOS/Linux. The official MetaTrader5 package is")
        print("Windows-only. Run this CLI on a Windows MT5 host/VPS where the")
        print("FundingPips terminal is installed.")
        return EXIT_UNSUPPORTED_ENV

    if isinstance(exc, MissingCredentialError):
        print()
        print("MISSING CREDENTIALS")
        print("-" * 19)
        print(message)
        print()
        print("Copy .env.example to .env (gitignored) and fill in:")
        print("    FUNDINGPIPS_MT5_LOGIN      your MT5 ACCOUNT NUMBER (not the email)")
        print("    FUNDINGPIPS_MT5_PASSWORD   the TRADING password")
        print("    FUNDINGPIPS_MT5_SERVER     FundingPips-Trial")
        return EXIT_MISSING_CREDENTIALS

    print()
    print("FAILED: %s" % name)
    print("-" * (8 + len(name)))
    print(message)
    if isinstance(exc, ExecutionError) and exc.context:
        for key, value in exc.context.items():
            print("    %s = %s" % (key, value))
    return EXIT_FAILURE


def build_adapter(allow_order: bool = False, max_spread_pips: Optional[float] = None,
                  mock: bool = False):
    """Construct the adapter WITHOUT connecting. Read-only by default.

    When mock=True, an in-memory FakeMetaTrader5 gateway is injected. This allows
    operators on macOS/Linux to verify the entire dry-run preflight, order submission,
    position tracking, position closure, and cost attribution lifecycle without
    needing a Windows MT5 host immediately.
    """
    from src.execution.fundingpips_mt5 import FundingPipsMT5Adapter

    if mock:
        from tests.execution.fake_mt5 import fakes_env, make_fake
        env = fakes_env(FUNDINGPIPS_ALLOW_TRADING="true" if allow_order else "false")
        return FundingPipsMT5Adapter.from_env(
            env=env,
            mt5_module=make_fake(),
            allow_order=allow_order,
            max_spread_pips=max_spread_pips,
        )

    return FundingPipsMT5Adapter.from_env(
        allow_order=allow_order,
        max_spread_pips=max_spread_pips,
    )


def connect_and_report(adapter) -> Optional[int]:
    """Connect; on failure print the diagnostic and return an exit code."""
    try:
        adapter.connect()
    except BaseException as exc:  # noqa: BLE001 - CLI boundary
        return report_failure(exc)
    return None


def safe_main(func) -> int:
    """Wrap a CLI entry point so no exception escapes un-redacted."""
    try:
        return int(func())
    except KeyboardInterrupt:
        print()
        print("Interrupted by user. Shutting down cleanly.")
        return EXIT_OK
    except BaseException as exc:  # noqa: BLE001 - CLI boundary
        return report_failure(exc)

