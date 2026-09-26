"""
FundingPips MT5 execution error model.

Domain-specific exceptions instead of bare RuntimeError, so that every failure is
auditable and testable. Every exception here is *safe by construction*: none of
them stores a password, and the constructor defensively redacts whatever message
it is handed. See :mod:src.execution.redaction.
"""
from __future__ import annotations

from typing import Any, Optional

from src.execution.redaction import redact


class ExecutionError(RuntimeError):
    """Base class for every execution-layer failure.

    The message is passed through redact() on the way in, so a caller that
    carelessly interpolates a credential into the message cannot leak it.
    """

    def __init__(self, message: str = "", **context: Any) -> None:
        self.context = {k: _redact_value(v) for k, v in context.items()}
        super().__init__(redact(str(message)))

    def __repr__(self) -> str:
        return "%s(%r, context=%r)" % (type(self).__name__, str(self), self.context)


def _redact_value(value: Any) -> Any:
    """Redact a context value while PRESERVING its structure.

    Blanket str() conversion would turn a candidate list into an unreadable string,
    which loses exactly the diagnostic detail an operator needs. Strings are still
    scrubbed; containers are walked recursively.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, (list, tuple)):
        return [_redact_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    return value


class MT5UnavailableError(ExecutionError):
    """The MetaTrader5 Python package is not importable in this environment.

    This is the honest answer on macOS/Linux. We do NOT fake a connection.
    """


class UnsupportedExecutionEnvironmentError(MT5UnavailableError):
    """MT5 Python integration cannot run natively on this OS/architecture."""


class MissingCredentialError(ExecutionError):
    """One or more required environment variables are absent or empty."""


class CredentialFormatError(ExecutionError):
    """A credential is present but malformed (e.g. login is not an integer)."""


class MT5InitializationError(ExecutionError):
    """mt5.initialize() returned False."""


class MT5AuthenticationError(ExecutionError):
    """mt5.login() returned False. Never retried automatically."""


class UnexpectedServerError(ExecutionError):
    """The terminal reported a server other than the expected one."""


class AccountMismatchError(ExecutionError):
    """The terminal logged into a different account than the one requested."""


class SymbolNotFoundError(ExecutionError):
    """No provider symbol could be resolved for the canonical symbol."""


class AmbiguousSymbolError(ExecutionError):
    """More than one equally plausible provider symbol exists. Fail, never guess."""


class TradingDisabledError(ExecutionError):
    """A write operation was attempted while trading is disabled."""


class OrderPreflightError(ExecutionError):
    """An order failed the centralised preflight check.

    Carries a structured reason_code so callers can branch without parsing prose.
    """

    def __init__(self, reason_code: str, message: str = "", **context: Any) -> None:
        self.reason_code = reason_code
        super().__init__(message or reason_code, **context)


class ConnectionLostError(ExecutionError):
    """The terminal dropped the connection mid-session."""


class DuplicateOrderError(ExecutionError):
    """An identical client order id / exposure already exists."""
