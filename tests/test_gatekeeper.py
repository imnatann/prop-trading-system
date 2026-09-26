"""
Unit Tests for Unified Risk Gatekeeper.
Memvalidasi integrasi seluruh filter proteksi sebelum eksekusi order.
"""

from datetime import datetime, timezone
import pytest

from config.prop_rules import PropFirmRules
from src.strategy.base import TradeSignal, SignalAction
from src.risk.drawdown_monitor import DrawdownMonitor, AccountSnapshot
from src.risk.gatekeeper import RiskGatekeeper
from src.data.news_calendar import EconomicEvent


@pytest.fixture
def gatekeeper_setup():
    rules = PropFirmRules(
        max_daily_loss_pct=4.0,
        max_open_trades=2,
        max_spread_pips=2.0,
        risk_per_trade_pct=0.5
    )
    monitor = DrawdownMonitor(initial_balance=100_000.0, max_daily_loss_pct=4.0)
    gatekeeper = RiskGatekeeper(rules=rules, drawdown_monitor=monitor)
    return gatekeeper, monitor


def test_gatekeeper_approves_safe_signal(gatekeeper_setup):
    gatekeeper, _ = gatekeeper_setup
    
    signal = TradeSignal(
        symbol="EURUSD",
        action=SignalAction.BUY,
        entry_price=1.08500,
        stop_loss=1.08300,
        take_profit=1.08900,
        rationale="Valid setup"
    )
    account = AccountSnapshot(balance=100_000.0, equity=100_000.0)
    
    decision = gatekeeper.evaluate(
        signal=signal,
        account=account,
        open_positions_count=0,
        current_spread_pips=1.2,
        economic_events=[]
    )
    
    assert decision.is_approved is True
    assert decision.lot_size == 2.50
    assert decision.rejection_reason is None


def test_gatekeeper_rejects_on_daily_dd_breach(gatekeeper_setup):
    gatekeeper, _ = gatekeeper_setup
    
    signal = TradeSignal(
        symbol="EURUSD",
        action=SignalAction.BUY,
        entry_price=1.08500,
        stop_loss=1.08300,
        take_profit=1.08900,
        rationale="Valid setup"
    )
    # Equity drop to $95,500 (Loss 4.5% > limit 4.0%)
    account = AccountSnapshot(balance=100_000.0, equity=95_500.0)
    
    decision = gatekeeper.evaluate(
        signal=signal,
        account=account,
        open_positions_count=0,
        current_spread_pips=1.2,
        economic_events=[]
    )
    
    assert decision.is_approved is False
    assert decision.lot_size == 0.0
    assert "Daily Drawdown breach" in decision.rejection_reason


def test_gatekeeper_rejects_on_max_open_trades(gatekeeper_setup):
    gatekeeper, _ = gatekeeper_setup
    
    signal = TradeSignal(
        symbol="EURUSD",
        action=SignalAction.BUY,
        entry_price=1.08500,
        stop_loss=1.08300,
        take_profit=1.08900,
        rationale="Valid setup"
    )
    account = AccountSnapshot(balance=100_000.0, equity=100_000.0)
    
    # max_open_trades = 2, posisi saat ini = 2
    decision = gatekeeper.evaluate(
        signal=signal,
        account=account,
        open_positions_count=2,
        current_spread_pips=1.2,
        economic_events=[]
    )
    
    assert decision.is_approved is False
    assert "Max open positions reached" in decision.rejection_reason


def test_gatekeeper_rejects_on_high_spread(gatekeeper_setup):
    gatekeeper, _ = gatekeeper_setup
    
    signal = TradeSignal(
        symbol="EURUSD",
        action=SignalAction.BUY,
        entry_price=1.08500,
        stop_loss=1.08300,
        take_profit=1.08900,
        rationale="Valid setup"
    )
    account = AccountSnapshot(balance=100_000.0, equity=100_000.0)
    
    # Spread 3.5 pips > max 2.0 pips
    decision = gatekeeper.evaluate(
        signal=signal,
        account=account,
        open_positions_count=0,
        current_spread_pips=3.5,
        economic_events=[]
    )
    
    assert decision.is_approved is False
    assert "Spread too high" in decision.rejection_reason
