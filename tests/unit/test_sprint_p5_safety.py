import os
import tempfile
import time
import pytest
from datetime import datetime, timezone

from config.prop_rules import PropFirmRules
from src.broker.base import PositionInfo
from src.execution.broker_adapter import MockBrokerAdapter
from src.risk.drawdown_monitor import DrawdownMonitor
from src.safety.heartbeat import HeartbeatManager
from src.safety.kill_switch import EmergencyKillSwitch
from src.safety.watchdog import RiskWatchdog


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


def test_heartbeat_manager_write_and_liveness(temp_dir):
    hb = HeartbeatManager(directory=temp_dir)
    assert hb.is_alive("trading_engine", max_stale_seconds=5.0) is False

    hb.write_heartbeat("trading_engine", {"status": "RUNNING"})
    assert hb.is_alive("trading_engine", max_stale_seconds=5.0) is True

    payload = hb.read_heartbeat("trading_engine")
    assert payload is not None
    assert payload["component"] == "trading_engine"
    assert payload["payload"]["status"] == "RUNNING"

    # With very small max_stale_seconds and a tiny delay, test staleness
    time.sleep(0.05)
    assert hb.is_alive("trading_engine", max_stale_seconds=0.01) is False


def test_risk_watchdog_healthy_cycle(temp_dir):
    broker = MockBrokerAdapter(initial_balance=100_000.0)
    broker.connect()
    rules = PropFirmRules()
    dd_monitor = DrawdownMonitor(initial_balance=100_000.0)
    hb = HeartbeatManager(directory=temp_dir)
    lock_file = os.path.join(temp_dir, "halt.lock")
    kill_switch = EmergencyKillSwitch(broker, rules, dd_monitor, lock_file_path=lock_file)

    # Engine is alive
    hb.write_heartbeat("trading_engine", {"status": "OK"})

    watchdog = RiskWatchdog(
        broker=broker,
        rules=rules,
        drawdown_monitor=dd_monitor,
        heartbeat_manager=hb,
        kill_switch=kill_switch,
        max_stale_heartbeat_seconds=5.0
    )

    res = watchdog.poll_once()
    assert res.healthy is True
    assert res.engine_alive is True
    assert res.equity_safe is True
    assert res.action_taken == "NONE"
    assert not os.path.exists(lock_file)


def test_risk_watchdog_dead_engine_with_open_positions_triggers_liquidation(temp_dir):
    broker = MockBrokerAdapter(initial_balance=100_000.0)
    broker.connect()
    rules = PropFirmRules()
    dd_monitor = DrawdownMonitor(initial_balance=100_000.0)
    hb = HeartbeatManager(directory=temp_dir)
    lock_file = os.path.join(temp_dir, "halt.lock")
    kill_switch = EmergencyKillSwitch(broker, rules, dd_monitor, lock_file_path=lock_file)

    # Broker has open position
    broker.positions.append(PositionInfo(
        position_id="ACTIVE-1",
        client_order_id="QP-1",
        symbol="EURUSD",
        action="BUY",
        volume=0.5,
        entry_price=1.0850,
        sl=1.0800,
        tp=1.0950,
        current_price=1.0850,
        profit=0.0,
        open_time=datetime.now(timezone.utc)
    ))
    assert broker.get_open_positions_count() == 1

    # But trading engine has NO heartbeat (crashed / hanged)
    watchdog = RiskWatchdog(
        broker=broker,
        rules=rules,
        drawdown_monitor=dd_monitor,
        heartbeat_manager=hb,
        kill_switch=kill_switch,
        max_stale_heartbeat_seconds=1.0,
        auto_liquidate_on_dead_engine=True
    )

    res = watchdog.poll_once()
    assert res.healthy is False
    assert res.engine_alive is False
    assert res.action_taken == "ORPHAN_LIQUIDATE_AND_HALT"

    # Positions liquidated to protect account
    assert broker.get_open_positions_count() == 0
    assert os.path.exists(lock_file)


def test_risk_watchdog_critical_drawdown_breach(temp_dir):
    broker = MockBrokerAdapter(initial_balance=100_000.0)
    broker.connect()
    rules = PropFirmRules()
    dd_monitor = DrawdownMonitor(initial_balance=100_000.0, max_daily_loss_pct=5.0)
    dd_monitor.reset_daily_baseline(100_000.0, 100_000.0, force=True)
    hb = HeartbeatManager(directory=temp_dir)
    lock_file = os.path.join(temp_dir, "halt.lock")
    kill_switch = EmergencyKillSwitch(broker, rules, dd_monitor, lock_file_path=lock_file)

    # Engine is running
    hb.write_heartbeat("trading_engine")

    # Equity collapses to 93,000 (7% drawdown > 4.5% kill switch limit)
    broker.set_mock_equity(93_000.0)

    watchdog = RiskWatchdog(
        broker=broker,
        rules=rules,
        drawdown_monitor=dd_monitor,
        heartbeat_manager=hb,
        kill_switch=kill_switch
    )

    res = watchdog.poll_once()
    assert res.healthy is False
    assert res.equity_safe is False
    assert res.action_taken == "EMERGENCY_KILL_SWITCH"
    assert os.path.exists(lock_file)
