"""Order preflight and the two-gate safety model.

Covers required cases 12-20, 27, 28: lot bounds, lot-step alignment, stops level,
trading disabled by default, and the requirement that BOTH an environment flag and an
explicit order flag be present before any write reaches the broker.
"""
from __future__ import annotations

import pytest

from src.execution.errors import (
    OrderPreflightError,
    TradingDisabledError,
)
from src.execution.fundingpips_mt5 import FundingPipsMT5Adapter
from src.execution.models import OrderIntent, OrderSide, OrderStatus, TradeMode
from src.execution.redaction import clear_registered_secrets
from tests.execution.fake_mt5 import (
    FakeAccount,
    FakeMetaTrader5,
    FakePosition,
    FakeSymbol,
    FakeTick,
    fakes_env,
    make_fake,
)


@pytest.fixture(autouse=True)
def _clean_secrets():
    clear_registered_secrets()
    yield
    clear_registered_secrets()


def build(allow_trading=False, allow_order=False, fake=None, symbols=None, **kw) -> FundingPipsMT5Adapter:
    env = fakes_env(**({"FUNDINGPIPS_ALLOW_TRADING": "true"} if allow_trading else {}))
    adapter = FundingPipsMT5Adapter.from_env(env=env, mt5_module=fake or make_fake(),
                                             allow_order=allow_order, **kw)
    adapter.connect()
    return adapter


def intent(**overrides) -> OrderIntent:
    base = dict(symbol="EURUSD", side=OrderSide.BUY, volume=0.01)
    base.update(overrides)
    return OrderIntent(**base)


# ------------------------------------------------ case 16: disabled by default
def test_place_order_disabled_by_default():
    adapter = build()
    with pytest.raises(TradingDisabledError):
        adapter.place_order(intent())


def test_close_position_disabled_by_default():
    adapter = build()
    with pytest.raises(TradingDisabledError):
        adapter.close_position("1001")


def test_modify_position_disabled_by_default():
    adapter = build()
    with pytest.raises(TradingDisabledError):
        adapter.modify_position("1001", 0.0, 0.0)


def test_cancel_order_disabled_by_default():
    adapter = build()
    with pytest.raises(TradingDisabledError):
        adapter.cancel_order("2001")


def test_disabled_adapter_sends_no_order():
    fake = make_fake()
    adapter = build(fake=fake)
    with pytest.raises(TradingDisabledError):
        adapter.place_order(intent())
    assert fake.call_count("order_send") == 0


def test_read_only_operations_allowed_while_disabled():
    """The gate must not block reads; only writes."""
    adapter = build()
    assert adapter.account_info().login == 12345678
    assert adapter.symbol_info("EURUSD").provider_symbol == "EURUSD"
    assert adapter.tick("EURUSD").is_valid
    assert adapter.positions() == []
    assert adapter.orders() == []


# --------------------------------- case 17: env flag alone is NOT sufficient
def test_environment_flag_alone_insufficient():
    fake = make_fake()
    adapter = build(allow_trading=True, allow_order=False, fake=fake)
    assert adapter.trading_enabled is True
    assert adapter.can_trade is False
    with pytest.raises(TradingDisabledError) as excinfo:
        adapter.place_order(intent())
    assert "explicit" in str(excinfo.value).lower()
    assert fake.call_count("order_send") == 0


# --------------------------------- case 18: order flag alone is NOT sufficient
def test_explicit_order_flag_alone_insufficient():
    fake = make_fake()
    adapter = build(allow_trading=False, allow_order=True, fake=fake)
    assert adapter.can_trade is False
    with pytest.raises(TradingDisabledError):
        adapter.place_order(intent())
    assert fake.call_count("order_send") == 0


# --------------------------------- case 19: both gates required
def test_both_gates_required():
    fake = make_fake()
    adapter = build(allow_trading=True, allow_order=True, fake=fake)
    assert adapter.can_trade is True
    result = adapter.place_order(intent())
    assert result.success is True
    assert fake.call_count("order_send") == 1


def test_preflight_reports_reason_codes_for_missing_gates():
    adapter = build(allow_trading=False, allow_order=False)
    with pytest.raises(OrderPreflightError) as excinfo:
        adapter.preflight(intent())
    assert excinfo.value.reason_code == "trading_disabled"

    adapter2 = build(allow_trading=True, allow_order=False)
    with pytest.raises(OrderPreflightError) as excinfo2:
        adapter2.preflight(intent())
    assert excinfo2.value.reason_code == "explicit_order_flag_required"


# ---------------------------------------------------- case 12: min lot
def test_volume_below_minimum_rejected():
    adapter = build(allow_trading=True, allow_order=True)
    with pytest.raises(OrderPreflightError) as excinfo:
        adapter.preflight(intent(volume=0.001))
    assert excinfo.value.reason_code == "volume_below_minimum"


def test_volume_minimum_is_not_silently_raised():
    """Prefer reject over repair: the adapter must never bump 0.001 up to 0.01."""
    fake = make_fake()
    adapter = build(allow_trading=True, allow_order=True, fake=fake)
    with pytest.raises(OrderPreflightError):
        adapter.preflight(intent(volume=0.001))
    assert fake.call_count("order_send") == 0


def test_volume_at_minimum_accepted():
    adapter = build(allow_trading=True, allow_order=True)
    adapter.preflight(intent(volume=0.01))


# ---------------------------------------------------- case 13: lot step
def test_volume_not_aligned_to_step_rejected():
    fake = make_fake(symbols=[FakeSymbol(name="EURUSD", volume_min=0.01, volume_step=0.01)])
    adapter = build(allow_trading=True, allow_order=True, fake=fake)
    with pytest.raises(OrderPreflightError) as excinfo:
        adapter.preflight(intent(volume=0.015))
    assert excinfo.value.reason_code == "volume_not_aligned_to_step"


def test_volume_aligned_to_step_accepted():
    adapter = build(allow_trading=True, allow_order=True)
    adapter.preflight(intent(volume=0.03))


def test_volume_step_is_not_silently_repaired():
    fake = make_fake()
    adapter = build(allow_trading=True, allow_order=True, fake=fake)
    with pytest.raises(OrderPreflightError):
        adapter.preflight(intent(volume=0.017))
    assert fake.call_count("order_send") == 0


# ---------------------------------------------------- case 14: max lot
def test_volume_above_maximum_rejected():
    fake = make_fake(symbols=[FakeSymbol(name="EURUSD", volume_max=5.0)])
    adapter = build(allow_trading=True, allow_order=True, fake=fake)
    with pytest.raises(OrderPreflightError) as excinfo:
        adapter.preflight(intent(volume=10.0))
    assert excinfo.value.reason_code == "volume_above_maximum"


def test_non_positive_volume_rejected():
    adapter = build(allow_trading=True, allow_order=True)
    with pytest.raises(OrderPreflightError) as excinfo:
        adapter.preflight(intent(volume=0.0))
    assert excinfo.value.reason_code == "volume_non_positive"


# ---------------------------------------------------- case 15: stops level
def test_stop_loss_inside_stops_level_rejected():
    fake = make_fake(symbols=[FakeSymbol(name="EURUSD", trade_stops_level=100)], tick=FakeTick(bid=1.08500, ask=1.08512))
    adapter = build(allow_trading=True, allow_order=True, fake=fake)
    # 100 points * 0.00001 = 0.001 minimum; SL 0.0002 away is too close
    with pytest.raises(OrderPreflightError) as excinfo:
        adapter.preflight(intent(sl=1.08480))
    assert excinfo.value.reason_code == "stop_loss_too_close"


def test_take_profit_inside_stops_level_rejected():
    fake = make_fake(symbols=[FakeSymbol(name="EURUSD", trade_stops_level=100)], tick=FakeTick(bid=1.08500, ask=1.08512))
    adapter = build(allow_trading=True, allow_order=True, fake=fake)
    with pytest.raises(OrderPreflightError) as excinfo:
        adapter.preflight(intent(tp=1.08530))
    assert excinfo.value.reason_code == "take_profit_too_close"


def test_stop_distance_beyond_stops_level_accepted():
    fake = make_fake(symbols=[FakeSymbol(name="EURUSD", trade_stops_level=100)], tick=FakeTick(bid=1.08500, ask=1.08512))
    adapter = build(allow_trading=True, allow_order=True, fake=fake)
    adapter.preflight(intent(sl=1.08000, tp=1.09000))


def test_zero_stops_level_imposes_no_minimum():
    adapter = build(allow_trading=True, allow_order=True)
    adapter.preflight(intent(sl=1.08499, tp=1.08501))


# ---------------------------------------------------- direction / trade mode
def test_short_only_symbol_rejects_buy():
    fake = make_fake(symbols=[FakeSymbol(name="EURUSD", trade_mode=2)])
    adapter = build(allow_trading=True, allow_order=True, fake=fake)
    with pytest.raises(OrderPreflightError) as excinfo:
        adapter.preflight(intent(side=OrderSide.BUY))
    assert excinfo.value.reason_code == "direction_not_allowed"


def test_long_only_symbol_rejects_sell():
    fake = make_fake(symbols=[FakeSymbol(name="EURUSD", trade_mode=1)])
    adapter = build(allow_trading=True, allow_order=True, fake=fake)
    with pytest.raises(OrderPreflightError) as excinfo:
        adapter.preflight(intent(side=OrderSide.SELL))
    assert excinfo.value.reason_code == "direction_not_allowed"


def test_disabled_symbol_rejected():
    fake = make_fake(symbols=[FakeSymbol(name="EURUSD", trade_mode=0)])
    adapter = build(allow_trading=True, allow_order=True, fake=fake)
    with pytest.raises(OrderPreflightError) as excinfo:
        adapter.preflight(intent())
    assert excinfo.value.reason_code == "symbol_trade_disabled"


def test_close_only_symbol_rejected_for_new_order():
    fake = make_fake(symbols=[FakeSymbol(name="EURUSD", trade_mode=3)])
    adapter = build(allow_trading=True, allow_order=True, fake=fake)
    with pytest.raises(OrderPreflightError) as excinfo:
        adapter.preflight(intent())
    assert excinfo.value.reason_code == "symbol_close_only"


# ---------------------------------------------------- spread ceiling
def test_spread_above_ceiling_rejected():
    fake = make_fake(tick=FakeTick(bid=1.08500, ask=1.08600))  # 10 pips
    adapter = build(allow_trading=True, allow_order=True, fake=fake, max_spread_pips=3.0)
    with pytest.raises(OrderPreflightError) as excinfo:
        adapter.preflight(intent())
    assert excinfo.value.reason_code == "spread_above_ceiling"


def test_invalid_quote_rejected():
    fake = make_fake(tick=FakeTick(bid=1.08512, ask=1.08500))  # crossed
    adapter = build(allow_trading=True, allow_order=True, fake=fake)
    with pytest.raises(OrderPreflightError) as excinfo:
        adapter.preflight(intent())
    assert excinfo.value.reason_code == "invalid_quote"


def test_zero_quote_rejected():
    fake = make_fake(tick=FakeTick(bid=0.0, ask=0.0))
    adapter = build(allow_trading=True, allow_order=True, fake=fake)
    with pytest.raises(OrderPreflightError) as excinfo:
        adapter.preflight(intent())
    assert excinfo.value.reason_code == "invalid_quote"


# ------------------------------------------------ account trading permission
def test_account_trading_disallowed_rejected():
    fake = make_fake(account=FakeAccount(trade_allowed=False))
    adapter = build(allow_trading=True, allow_order=True, fake=fake)
    with pytest.raises(OrderPreflightError) as excinfo:
        adapter.preflight(intent())
    assert excinfo.value.reason_code == "account_trading_disallowed"


# ------------------------------------------- case 27: duplicate prevention
def test_duplicate_client_order_id_rejected():
    fake = make_fake(positions=[FakePosition(ticket=1001, comment="QP-SMOKE-1")])
    adapter = build(allow_trading=True, allow_order=True, fake=fake)
    # Duplicate detection is by client_order_id carried in the position comment.
    from src.execution.models import PositionSnapshot

    with pytest.raises(OrderPreflightError) as excinfo:
        adapter.preflight(intent(client_order_id="QP-SMOKE-1"))
    assert excinfo.value.reason_code == "duplicate_client_order_id"


def test_unique_client_order_id_allowed():
    fake = make_fake(positions=[FakePosition(ticket=1001, comment="QP-SMOKE-1")])
    adapter = build(allow_trading=True, allow_order=True, fake=fake)
    adapter.preflight(intent(client_order_id="QP-SMOKE-2"))


# --------------------------------------- case 28: order result normalization
def test_rejected_order_normalizes_to_rejected_status():
    fake = make_fake(order_retcode=FakeMetaTrader5.TRADE_RETCODE_REJECT)
    adapter = build(allow_trading=True, allow_order=True, fake=fake)
    result = adapter.place_order(intent())
    assert result.success is False
    assert result.status is OrderStatus.REJECTED
    assert result.retcode == FakeMetaTrader5.TRADE_RETCODE_REJECT


def test_accepted_order_normalizes_to_filled_status():
    adapter = build(allow_trading=True, allow_order=True)
    result = adapter.place_order(intent())
    assert result.success is True
    assert result.status is OrderStatus.FILLED
    assert result.order_id is not None
    assert result.retcode_name == "TRADE_RETCODE_DONE"


def test_preflight_failure_returns_rejected_result_not_exception():
    """place_order converts a preflight rejection into a structured OrderResult."""
    fake = make_fake()
    adapter = build(allow_trading=True, allow_order=True, fake=fake)
    result = adapter.place_order(intent(volume=0.001))
    assert result.success is False
    assert result.status is OrderStatus.REJECTED
    assert "volume_below_minimum" in (result.comment or "")
    assert fake.call_count("order_send") == 0


def test_filling_mode_chosen_from_symbol_mask():
    """A symbol declaring IOC-only must not be sent an FOK request."""
    # filling_mode is a SYMBOL_FILLING_* bitmask: 2 == IOC only
    fake = make_fake(symbols=[FakeSymbol(name="EURUSD", filling_mode=2)])
    adapter = build(allow_trading=True, allow_order=True, fake=fake)
    adapter.place_order(intent())
    request = fake.order_requests[0]
    assert request.get("type_filling") == FakeMetaTrader5.ORDER_FILLING_IOC


def test_order_uses_provider_symbol():
    fake = make_fake(symbols=[FakeSymbol(name="EURUSD.r")])
    adapter = build(allow_trading=True, allow_order=True, fake=fake)
    adapter.place_order(intent())
    assert fake.order_requests[0]["symbol"] == "EURUSD.r"


def test_order_request_never_contains_credentials():
    fake = make_fake()
    adapter = build(allow_trading=True, allow_order=True, fake=fake)
    adapter.place_order(intent(client_order_id="QP-1"))
    request = fake.order_requests[0]
    assert "password" not in str(request).lower()
    assert "trading-pw" not in str(request)

