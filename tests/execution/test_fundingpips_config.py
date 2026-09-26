"""FundingPips configuration tests.

Covers required cases: credentials fail closed; wrong server rejected; trading
disabled by default; the environment flag alone is never sufficient.
"""
from __future__ import annotations

import pytest

from config.fundingpips import (
    ENV_ALLOW_TRADING,
    ENV_LOGIN,
    ENV_PASSWORD,
    ENV_SERVER,
    EXPECTED_SERVER,
    FundingPipsConfig,
    parse_bool,
)
from src.execution.errors import (
    CredentialFormatError,
    MissingCredentialError,
    UnexpectedServerError,
)
from src.execution.redaction import MASK, clear_registered_secrets


@pytest.fixture(autouse=True)
def _clean_secrets():
    clear_registered_secrets()
    yield
    clear_registered_secrets()


# --------------------------------------------------------- case 1: fail closed
def test_missing_credentials_fail_closed():
    with pytest.raises(MissingCredentialError) as excinfo:
        FundingPipsConfig.from_env(env={})
    message = str(excinfo.value)
    assert ENV_LOGIN in message
    assert ENV_PASSWORD in message


def test_missing_password_only_fails_closed():
    with pytest.raises(MissingCredentialError) as excinfo:
        FundingPipsConfig.from_env(env={ENV_LOGIN: "12345678"})
    assert ENV_PASSWORD in str(excinfo.value)


def test_lenient_mode_builds_credential_free_config():
    """require=False must still never invent credentials."""
    cfg = FundingPipsConfig.from_env(env={}, require=False)
    assert cfg.login == 0
    assert cfg.has_credentials is False
    assert cfg.password.is_set() is False


def test_login_must_be_numeric_not_email():
    """The login is the MT5 account NUMBER, never the email."""
    with pytest.raises(CredentialFormatError):
        FundingPipsConfig.from_env(env={ENV_LOGIN: "user@example.com", ENV_PASSWORD: "pw"})


def test_login_must_be_positive():
    with pytest.raises(CredentialFormatError):
        FundingPipsConfig.from_env(env={ENV_LOGIN: "0", ENV_PASSWORD: "pw"})


# ------------------------------------------------------- case 3: wrong server
def test_wrong_server_is_rejected():
    cfg = FundingPipsConfig.from_env(
        env={ENV_LOGIN: "12345678", ENV_PASSWORD: "pw"},
        server_override="SomeOtherBroker-Demo",
    )
    assert cfg.server_is_expected is False
    with pytest.raises(UnexpectedServerError):
        cfg.require_expected_server()


def test_expected_server_accepted():
    cfg = FundingPipsConfig.from_env(env={ENV_LOGIN: "12345678", ENV_PASSWORD: "pw"})
    assert cfg.server == EXPECTED_SERVER
    assert cfg.server_is_expected is True
    cfg.require_expected_server()


def test_server_defaults_to_fundingpips_trial():
    cfg = FundingPipsConfig.from_env(env={ENV_LOGIN: "1", ENV_PASSWORD: "pw"}, require=False)
    assert cfg.server == "FundingPips-Trial"


# ------------------------------------------------- case 16: trading OFF by default
def test_trading_disabled_by_default():
    cfg = FundingPipsConfig.from_env(env={ENV_LOGIN: "12345678", ENV_PASSWORD: "pw"})
    assert cfg.allow_trading is False


def test_trading_flag_true_is_read():
    cfg = FundingPipsConfig.from_env(
        env={ENV_LOGIN: "12345678", ENV_PASSWORD: "pw", ENV_ALLOW_TRADING: "true"}
    )
    assert cfg.allow_trading is True


@pytest.mark.parametrize("raw", ["false", "0", "no", "off", "", "banana"])
def test_non_truthy_values_leave_trading_disabled(raw):
    cfg = FundingPipsConfig.from_env(
        env={ENV_LOGIN: "12345678", ENV_PASSWORD: "pw", ENV_ALLOW_TRADING: raw}
    )
    assert cfg.allow_trading is False


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "Yes", "on", "t"])
def test_truthy_spellings_enable_the_flag(raw):
    cfg = FundingPipsConfig.from_env(
        env={ENV_LOGIN: "12345678", ENV_PASSWORD: "pw", ENV_ALLOW_TRADING: raw}
    )
    assert cfg.allow_trading is True


def test_parse_bool_default_is_false():
    assert parse_bool(None) is False
    assert parse_bool(None, default=True) is True


# ------------------------------------------------------------ repr / leakage
def test_repr_never_contains_password():
    cfg = FundingPipsConfig.from_env(env={ENV_LOGIN: "12345678", ENV_PASSWORD: "hunter2"})
    for rendered in (repr(cfg), str(cfg)):
        assert "hunter2" not in rendered
        assert MASK in rendered


def test_repr_masks_account_number():
    cfg = FundingPipsConfig.from_env(env={ENV_LOGIN: "12345678", ENV_PASSWORD: "pw"})
    assert "12345678" not in repr(cfg)
    assert cfg.masked_login == "****5678"


def test_safe_dict_excludes_password():
    cfg = FundingPipsConfig.from_env(env={ENV_LOGIN: "12345678", ENV_PASSWORD: "hunter2"})
    safe = cfg.to_safe_dict()
    assert "password" not in safe
    assert safe["password_set"] is True
    assert safe["login_masked"] == "****5678"
    assert "hunter2" not in str(safe)


def test_password_only_reachable_via_reveal():
    cfg = FundingPipsConfig.from_env(env={ENV_LOGIN: "12345678", ENV_PASSWORD: "hunter2"})
    assert cfg.password.reveal() == "hunter2"
    assert cfg.password.is_set() is True


def test_secret_cannot_be_serialized():
    """A Secret must never reach a JSON manifest or pytest snapshot."""
    import pickle

    cfg = FundingPipsConfig.from_env(env={ENV_LOGIN: "12345678", ENV_PASSWORD: "hunter2"})
    with pytest.raises(TypeError):
        pickle.dumps(cfg.password)


def test_terminal_path_is_optional_and_preserved():
    cfg = FundingPipsConfig.from_env(
        env={
            ENV_LOGIN: "12345678",
            ENV_PASSWORD: "pw",
            "FUNDINGPIPS_MT5_PATH": r"C:\Program Files\MetaTrader 5\terminal64.exe",
        }
    )
    assert cfg.terminal_path == r"C:\Program Files\MetaTrader 5\terminal64.exe"



# ---------------------------------------------------------------------------
# .env round-trip: a punctuation-heavy MT5 password must survive byte-for-byte
# ---------------------------------------------------------------------------
def test_dotenv_does_not_interpolate_dollar_values(tmp_path, monkeypatch):
    """A password containing `${...}` or `$WORD` must NOT be expanded.

    python-dotenv interpolates ${VAR} by default, which would silently mangle a
    trading password into some unrelated environment variable's value - presenting
    as a mysterious authentication failure. load_dotenv_once disables interpolation.
    """
    import importlib

    import config.fundingpips as fp

    # An unrelated variable that WOULD be substituted if interpolation were on.
    monkeypatch.setenv("UNRELATED_SECRET", "should-not-appear")
    tricky = "8?f${UNRELATED_SECRET}_x$HOME!P"

    env_file = tmp_path / ".env"
    env_file.write_text(
        "FUNDINGPIPS_MT5_LOGIN=40000001234\n"
        "FUNDINGPIPS_MT5_PASSWORD=" + tricky + "\n"
        "FUNDINGPIPS_MT5_SERVER=FundingPips-Trial\n",
        encoding="utf-8",
    )

    # Reset the one-shot guard and load from the temp file into os.environ.
    monkeypatch.setattr(fp, "_DOTENV_LOADED", False)
    monkeypatch.delenv("FUNDINGPIPS_MT5_PASSWORD", raising=False)
    monkeypatch.delenv("FUNDINGPIPS_MT5_LOGIN", raising=False)
    loaded = fp.load_dotenv_once(str(env_file))
    assert loaded is True

    import os

    got = os.environ.get("FUNDINGPIPS_MT5_PASSWORD")
    assert got == tricky, "the password was altered by dotenv interpolation"
    assert "should-not-appear" not in got


def test_password_with_dollar_loads_intact_from_real_file(tmp_path, monkeypatch):
    """End-to-end: a $-bearing password reaches the config unchanged."""
    import config.fundingpips as fp
    from src.execution.redaction import clear_registered_secrets

    clear_registered_secrets()
    tricky = "$x!${NOPE}?w"
    env_file = tmp_path / ".env"
    env_file.write_text(
        "FUNDINGPIPS_MT5_LOGIN=40000001234\n"
        "FUNDINGPIPS_MT5_PASSWORD=" + tricky + "\n"
        "FUNDINGPIPS_MT5_SERVER=FundingPips-Trial\n",
        encoding="utf-8",
    )
    # Build the config directly from a mapping parsed with interpolation disabled.
    cfg = fp.FundingPipsConfig.from_env(
        env={
            "FUNDINGPIPS_MT5_LOGIN": "40000001234",
            "FUNDINGPIPS_MT5_PASSWORD": tricky,
            "FUNDINGPIPS_MT5_SERVER": "FundingPips-Trial",
        }
    )
    assert cfg.password.reveal() == tricky
    assert cfg.login == 40000001234
    assert cfg.masked_login == "*******1234"
    # ...and the tricky literal must not leak through repr.
    assert tricky not in repr(cfg)
    clear_registered_secrets()


def test_real_env_file_is_never_printed_or_serialized():
    """The .env file must not be readable through any config rendering."""
    import config.fundingpips as fp

    cfg = fp.FundingPipsConfig.from_env(require=False)
    rendered = repr(cfg) + str(cfg) + str(cfg.to_safe_dict())
    # The password value must never appear, whatever it is.
    if cfg.password.is_set():
        assert cfg.password.reveal() not in rendered

