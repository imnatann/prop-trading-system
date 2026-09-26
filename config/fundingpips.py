"""
FundingPips (MetaTrader 5) execution configuration.

Credential policy - NON-NEGOTIABLE
----------------------------------
No credential is ever hardcoded, defaulted to a real value, printed, repr()-ed,
serialized, or passed on a command line. Everything arrives from the environment:

    FUNDINGPIPS_MT5_LOGIN       MT5 account NUMBER (not the email)
    FUNDINGPIPS_MT5_PASSWORD    the TRADING password (never the investor password)
    FUNDINGPIPS_MT5_SERVER      FundingPips-Trial

Optional:
    FUNDINGPIPS_MT5_PATH        path to terminal64.exe (Windows)
    FUNDINGPIPS_ALLOW_TRADING   "false" by default. See safety note below.

Safety model
------------
FUNDINGPIPS_ALLOW_TRADING alone can NEVER place an order. Real execution requires
BOTH:
    1. FUNDINGPIPS_ALLOW_TRADING=true          (configuration opt-in)
    2. an explicit --allow-order CLI flag      (operator opt-in)
This module deliberately exposes no way to bypass gate (2) from configuration.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from src.execution.errors import CredentialFormatError, MissingCredentialError
from src.execution.redaction import Secret, mask_account, redact, register_secret

#: The only server this integration targets.
EXPECTED_SERVER = "FundingPips-Trial"

ENV_LOGIN = "FUNDINGPIPS_MT5_LOGIN"
ENV_PASSWORD = "FUNDINGPIPS_MT5_PASSWORD"
ENV_SERVER = "FUNDINGPIPS_MT5_SERVER"
ENV_PATH = "FUNDINGPIPS_MT5_PATH"
ENV_ALLOW_TRADING = "FUNDINGPIPS_ALLOW_TRADING"

#: Recognised spellings of "true" for the trading flag.
_TRUTHY = {"1", "true", "yes", "on", "y", "t"}

#: Guards the one-time .env load so repeated construction is cheap and idempotent.
_DOTENV_LOADED = False


def load_dotenv_once(path: str = ".env") -> bool:
    """Load a gitignored .env into os.environ exactly once.

    The repository already keeps local secrets in a gitignored .env (see .env.example),
    and python-dotenv is already a transitive dependency via pydantic-settings. Values
    are loaded with override=False, so a real environment variable always WINS over the
    file. Nothing is logged, and the loaded values are never echoed.

    Why interpolate=False
    ---------------------
    python-dotenv expands `${VAR}` references by default. An MT5 trading password is
    arbitrary punctuation-heavy text, so a password containing `${...}` or a bare
    `$WORD` could be silently MANGLED into the value of some unrelated environment
    variable. That would present as an authentication failure with a credential that
    looks correct in the file. Interpolation is therefore disabled: the value in the
    file is used byte-for-byte.

    Returns True if a file was found and loaded.
    """
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return False
    _DOTENV_LOADED = True
    try:
        from dotenv import load_dotenv
    except ImportError:
        return False
    if not os.path.exists(path):
        return False
    try:
        load_dotenv(path, override=False, interpolate=False)
    except TypeError:  # pragma: no cover - older python-dotenv without the flag
        load_dotenv(path, override=False)
    return True


def _read_env(env: Optional[Mapping[str, str]], key: str) -> Optional[str]:
    """Read a variable from the supplied mapping or os.environ.

    An explicit mapping wins, which is what makes this testable without touching
    the real process environment.
    """
    source = os.environ if env is None else env
    raw = source.get(key)
    if raw is None:
        return None
    raw = str(raw).strip()
    return raw or None


def parse_bool(raw: Optional[str], default: bool = False) -> bool:
    """Parse a permissive boolean. Anything unrecognised falls back to default."""
    if raw is None:
        return default
    return str(raw).strip().lower() in _TRUTHY


@dataclass
class FundingPipsConfig:
    """Resolved FundingPips execution configuration.

    The password is held in a Secret wrapper, so repr()/str() of this dataclass is
    safe and there is no accidental f-string interpolation.
    """

    login: int
    password: Secret
    server: str = EXPECTED_SERVER
    terminal_path: Optional[str] = None
    allow_trading: bool = False
    symbol: str = "EURUSD"

    # ---------------------------------------------------------------- factory
    @classmethod
    def from_env(
        cls,
        env: Optional[Mapping[str, str]] = None,
        require: bool = True,
        symbol: str = "EURUSD",
        server_override: Optional[str] = None,
    ) -> "FundingPipsConfig":
        """Build the config from environment variables.

        Args:
            env: mapping to read instead of os.environ (used by tests).
            require: when True, missing login/password raise MissingCredentialError.
                When False the object is built with login=0 / empty password so the
                connection check can report a clean diagnostic instead of a traceback.
            symbol: canonical symbol to trade.
            server_override: CLI-supplied server, validated against EXPECTED_SERVER.

        Raises:
            MissingCredentialError: a required variable is absent while require=True.
            CredentialFormatError: the login is not a positive integer.
        """
        # Pick up a gitignored .env when reading the real process environment. An
        # explicit mapping (used by tests) bypasses this entirely.
        if env is None:
            load_dotenv_once()
        register_secret(_read_env(env, ENV_PASSWORD))

        raw_login = _read_env(env, ENV_LOGIN)
        raw_password = _read_env(env, ENV_PASSWORD)
        raw_server = server_override or _read_env(env, ENV_SERVER) or EXPECTED_SERVER
        path = _read_env(env, ENV_PATH)
        allow = parse_bool(_read_env(env, ENV_ALLOW_TRADING), default=False)

        missing = []
        if raw_login is None:
            missing.append(ENV_LOGIN)
        if raw_password is None:
            missing.append(ENV_PASSWORD)
        if missing and require:
            raise MissingCredentialError(
                "Missing required FundingPips environment variable(s): "
                + ", ".join(missing)
                + ". Set them in a gitignored .env file (see .env.example)."
            )

        login = 0
        if raw_login is not None:
            try:
                login = int(raw_login)
            except (TypeError, ValueError) as exc:
                raise CredentialFormatError(
                    "{} must be the numeric MT5 account number, not an email "
                    "or other text.".format(ENV_LOGIN)
                ) from exc
            if login <= 0:
                raise CredentialFormatError(
                    "{} must be a positive account number.".format(ENV_LOGIN)
                )

        return cls(
            login=login,
            password=Secret(raw_password or ""),
            server=raw_server,
            terminal_path=path,
            allow_trading=allow,
            symbol=symbol,
        )

    # --------------------------------------------------------------- derived
    @property
    def masked_login(self) -> str:
        return mask_account(self.login) if self.login else "****"

    @property
    def has_credentials(self) -> bool:
        return self.login > 0 and self.password.is_set()

    @property
    def server_is_expected(self) -> bool:
        return self.server == EXPECTED_SERVER

    def require_expected_server(self) -> None:
        """Raise if the configured server is not FundingPips-Trial."""
        from src.execution.errors import UnexpectedServerError

        if not self.server_is_expected:
            raise UnexpectedServerError(
                "Configured server %r is not the expected %r. Refusing to connect "
                "to an unknown endpoint; there is no silent fallback."
                % (redact(self.server), EXPECTED_SERVER)
            )

    def to_safe_dict(self) -> Dict[str, Any]:
        """Credential-free view. Safe for logs, JSON, and test snapshots."""
        return {
            "login_masked": self.masked_login,
            "server": self.server,
            "terminal_path": self.terminal_path,
            "allow_trading": self.allow_trading,
            "symbol": self.symbol,
            "password_set": self.password.is_set(),
        }

    def __repr__(self) -> str:
        return (
            "FundingPipsConfig(login=%s, server=%r, allow_trading=%s, symbol=%r, "
            "terminal_path=%r, password=%r)"
            % (
                self.masked_login,
                self.server,
                self.allow_trading,
                self.symbol,
                self.terminal_path,
                self.password,
            )
        )

    __str__ = __repr__

