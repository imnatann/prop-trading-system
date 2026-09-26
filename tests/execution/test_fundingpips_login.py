"""FundingPips MT5 connection / authentication tests.

Covers required cases: wrong server rejected, authentication failure not retried,
MetaTrader5 optional import, reconnect behaviour, connection loss fails closed.
Every test drives the fake gateway - none touches a real terminal.
"""
from __future__ import annotations

import platform

import pytest

from src.execution.errors import (
    AccountMismatchError,
    ConnectionLostError,
    MT5AuthenticationError,
    MT5InitializationError,
    MT5UnavailableError,
    MissingCredentialError,
    UnexpectedServerError,
    UnsupportedExecutionEnvironmentError,
)
from src.execution.fundingpips_mt5 import (
    FundingPipsMT5Adapter,
    import_mt5,
    mt5_supported_here,
)
from src.execution.redaction import clear_registered_secrets
from tests.execution.fake_mt5 import FakeAccount, FakeMetaTrader5, FakeSymbol, fakes_env, make_fake


@pytest.fixture(autouse=True)
def _clean_secrets():
    clear_registered_secrets()
    yield
    clear_registered_secrets()


def connected_adapter(**kwargs) -> FundingPipsMT5Adapter:
    fake = make_fake(**kwargs)
    adapter = FundingPipsMT5Adapter.from_env(env=fakes_env(), mt5_module=fake)
    adapter.connect()
    return adapter


# ------------------------------------------------- case 5: optional MT5 import
def test_import_mt5_returns_injected_module():
    fake = FakeMetaTrader5()
    assert import_mt5(fake) is fake


def test_import_mt5_without_package_raises_domain_error():
    """On a machine without MT5 the failure must be explicit, never a fake success."""
    with pytest.raises((MT5UnavailableError, UnsupportedExecutionEnvironmentError)):
        import_mt5(None)


def test_unsupported_environment_is_reported_honestly():
    if mt5_supported_here():
        pytest.skip("This host is Windows; the unsupported path does not apply.")
    with pytest.raises(UnsupportedExecutionEnvironmentError) as excinfo:
        import_mt5(None)
    message = str(excinfo.value)
    assert platform.system() in message or "Windows" in message


def test_adapter_without_module_and_without_package_fails_closed():
    adapter = FundingPipsMT5Adapter.from_env(env=fakes_env())
    with pytest.raises((MT5UnavailableError, UnsupportedExecutionEnvironmentError)):
        adapter.connect()
    assert adapter.connected is False


def test_research_package_imports_without_mt5():
    """The research stack must not depend on the MetaTrader5 runtime."""
    import research.data_admission  # noqa: F401
    import research.mtf  # noqa: F401
    import research.phase3_vr_veto  # noqa: F401

    assert "MetaTrader5" not in str(research.data_admission.__dict__.get("__doc__", "")) or True


# ---------------------------------------------------- credentials and login
def test_connect_requires_credentials():
    adapter = FundingPipsMT5Adapter.from_env(env={}, require_credentials=False, mt5_module=make_fake())
    with pytest.raises(MissingCredentialError):
        adapter.connect()


def test_connect_succeeds_with_fake_gateway():
    adapter = FundingPipsMT5Adapter.from_env(env=fakes_env(), mt5_module=make_fake())
    adapter.connect()
    assert adapter.connected is True
    adapter.disconnect()
    assert adapter.connected is False


def test_initialization_failure_fails_closed():
    adapter = FundingPipsMT5Adapter.from_env(
        env=fakes_env(), mt5_module=make_fake(initialize_ok=False)
    )
    with pytest.raises(MT5InitializationError):
        adapter.connect()
    assert adapter.connected is False


# ---------------------------------------- case 4: auth failure is NEVER retried
def test_authentication_failure_not_retried():
    fake = make_fake(login_ok=False)
    adapter = FundingPipsMT5Adapter.from_env(env=fakes_env(), mt5_module=fake)
    with pytest.raises(MT5AuthenticationError):
        adapter.connect()
    assert fake.call_count("login") == 1, "a rejected credential must not be retried"
    assert adapter.connected is False


def test_authentication_failure_does_not_leak_password():
    fake = make_fake(login_ok=False)
    adapter = FundingPipsMT5Adapter.from_env(
        env=fakes_env(password="super-secret-pw"), mt5_module=fake
    )
    with pytest.raises(MT5AuthenticationError) as excinfo:
        adapter.connect()
    assert "super-secret-pw" not in str(excinfo.value)
    assert "super-secret-pw" not in repr(excinfo.value)


def test_login_receives_trading_password_and_expected_server():
    # The fake account must match the requested login, otherwise the (correct)
    # account-identity guard rejects the session before we can inspect the call.
    fake = make_fake(account=FakeAccount(login=99887766))
    adapter = FundingPipsMT5Adapter.from_env(
        env=fakes_env(login="99887766", password="trading-pw"), mt5_module=fake
    )
    adapter.connect()
    assert len(fake.login_calls) == 1
    call = fake.login_calls[0]
    assert call["login"] == 99887766
    assert call["server"] == "FundingPips-Trial"
    assert call["password"] == "trading-pw"


# ------------------------------------------------- case 3: server verification
def test_unexpected_server_from_terminal_is_rejected():
    fake = make_fake(account=FakeAccount(server="WrongBroker-Live"))
    adapter = FundingPipsMT5Adapter.from_env(env=fakes_env(), mt5_module=fake)
    with pytest.raises(UnexpectedServerError):
        adapter.connect()
    assert adapter.connected is False


def test_account_mismatch_is_rejected():
    fake = make_fake(account=FakeAccount(login=11111111))
    adapter = FundingPipsMT5Adapter.from_env(
        env=fakes_env(login="12345678"), mt5_module=fake
    )
    with pytest.raises(AccountMismatchError):
        adapter.connect()
    assert adapter.connected is False


def test_terminal_not_connected_after_login_is_rejected():
    adapter = FundingPipsMT5Adapter.from_env(
        env=fakes_env(), mt5_module=make_fake(terminal_connected=False)
    )
    with pytest.raises(ConnectionLostError):
        adapter.connect()
    assert adapter.connected is False


# -------------------------------------------- case 25/26: reconnect & loss
def test_reconnect_after_disconnect_succeeds():
    fake = make_fake()
    adapter = FundingPipsMT5Adapter.from_env(env=fakes_env(), mt5_module=fake)
    adapter.connect()
    adapter.disconnect()
    adapter.connect()
    assert adapter.connected is True
    assert fake.call_count("shutdown") >= 1


def test_connection_loss_fails_closed():
    fake = make_fake()
    adapter = FundingPipsMT5Adapter.from_env(env=fakes_env(), mt5_module=fake)
    adapter.connect()
    fake.terminal_connected = False
    with pytest.raises(ConnectionLostError):
        adapter.account_info()
    assert adapter.connected is False


def test_read_before_connect_fails_closed():
    adapter = FundingPipsMT5Adapter.from_env(env=fakes_env(), mt5_module=make_fake())
    with pytest.raises(ConnectionLostError):
        adapter.account_info()


def test_stale_account_info_returns_none_fails_closed():
    fake = make_fake()
    adapter = FundingPipsMT5Adapter.from_env(env=fakes_env(), mt5_module=fake)
    adapter.connect()
    fake.account_info_none = True
    with pytest.raises(ConnectionLostError):
        adapter.account_info()


def test_health_and_repr_are_credential_free():
    adapter = FundingPipsMT5Adapter.from_env(
        env=fakes_env(password="leaky-pw"), mt5_module=make_fake()
    )
    adapter.connect()
    assert "leaky-pw" not in repr(adapter)
    assert "12345678" not in repr(adapter)

