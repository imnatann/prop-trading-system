"""
Chaos Engineering & Adversarial Failure Injection Test Suite (Phase 14 / Sprint P16).
Menguji 7 skenario kegagalan operasional paling berbahaya:
1. Scenario 1: Engine dies with open positions -> Watchdog orphan liquidation + halt.lock.
2. Scenario 2: Broker network drop during submit -> UNKNOWN state & query recovery (anti blind-resend).
3. Scenario 3: Database failure / disk full / locked -> Fail-closed NO NEW RISK.
4. Scenario 4: Market data quote freeze -> FreshnessGate hard block.
5. Scenario 5: Split-Brain -> Second engine blocked by LeaderLock.
6. Scenario 6: Bad Config deployment -> ConfigValidator blocks startup.
7. Scenario 7: Clock drift anomaly -> ClockValidator fails & blocks trading.
"""

import os
import tempfile
import sqlite3
import pytest
from datetime import datetime, timedelta, timezone

from config.prop_rules import PropFirmRules
from config.validator import ConfigValidator
from src.broker.base import BaseBrokerAdapter, OrderResult, PositionInfo
from src.broker.clock_provider import ClockProvider
from src.broker.models import SymbolSpec
from src.data.market import Quote
from src.data.validation import FreshnessGate
from src.execution.broker_adapter import MockBrokerAdapter
from src.execution.dispatcher import OrderDispatcher
from src.oms.repository import OMSRepository
from src.oms.service import OMSService
from src.oms.states import OrderState
from src.risk.drawdown_monitor import AccountSnapshot, DrawdownMonitor
from src.risk.gatekeeper import RiskGatekeeper
from src.safety.heartbeat import HeartbeatManager
from src.safety.kill_switch import EmergencyKillSwitch
from src.safety.leader_lock import LeaderLock
from src.safety.startup_guard import StartupGuard
from src.safety.watchdog import RiskWatchdog
from src.strategy.base import SignalAction, TradeSignal


@pytest.fixture
def chaos_env():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "chaos_trading.db")
        hb_dir = os.path.join(tmpdir, "heartbeats")
        lock_file = os.path.join(tmpdir, "halt.lock")
        leader_lock_file = os.path.join(tmpdir, "trading_engine.lock")
        yield {
            "tmpdir": tmpdir,
            "db_path": db_path,
            "hb_dir": hb_dir,
            "lock_file": lock_file,
            "leader_lock_file": leader_lock_file
        }


# --- SCENARIO 1: ENGINE DIES WITH OPEN POSITIONS ---
def test_scenario_1_engine_dies_watchdog_orphan_liquidation(chaos_env):
    broker = MockBrokerAdapter(initial_balance=100_000.0)
    broker.connect()
    rules = PropFirmRules()
    dd_monitor = DrawdownMonitor(initial_balance=100_000.0)
    hb = HeartbeatManager(directory=chaos_env["hb_dir"])
    kill_switch = EmergencyKillSwitch(broker, rules, dd_monitor, lock_file_path=chaos_env["lock_file"])

    # Broker has open position
    broker.positions.append(PositionInfo(
        position_id="ACTIVE-TICKET-01", client_order_id="QP-ORPHAN", symbol="EURUSD",
        action="BUY", volume=0.5, entry_price=1.0850, sl=1.0800, tp=1.0950,
        current_price=1.0850, profit=0.0, open_time=datetime.now(timezone.utc)
    ))
    assert broker.get_open_positions_count() == 1

    # Simulate Trading Engine crash: NO heartbeat written, stale > threshold
    watchdog = RiskWatchdog(
        broker=broker,
        rules=rules,
        drawdown_monitor=dd_monitor,
        heartbeat_manager=hb,
        kill_switch=kill_switch,
        max_stale_heartbeat_seconds=0.1,
        auto_liquidate_on_dead_engine=True
    )

    res = watchdog.poll_once()
    assert res.healthy is False
    assert res.engine_alive is False
    assert res.action_taken == "ORPHAN_LIQUIDATE_AND_HALT"

    # Positions must be immediately liquidated to protect capital, and halt.lock created
    assert broker.get_open_positions_count() == 0
    assert os.path.exists(chaos_env["lock_file"])


# --- SCENARIO 2: BROKER NETWORK DROP DURING SUBMIT ---
def test_scenario_2_broker_network_drop_unknown_state_query_recovery(chaos_env):
    repo = OMSRepository(db_path=chaos_env["db_path"])
    oms = OMSService(repo)

    class DroppingBroker(BaseBrokerAdapter):
        def __init__(self):
            self._executed = {}

        def connect(self): return True
        def disconnect(self): pass
        def health(self): return {"status": "HEALTHY"}
        def get_server_time(self): return datetime.now(timezone.utc)
        def get_account_snapshot(self): return AccountSnapshot(100000, 100000, 0, 100000)
        def get_symbol_spec(self, symbol):
            return SymbolSpec(symbol, 0.00001, 1.0, 100000, 0.01, 50.0, 0.01, 5)
        def get_symbol_price(self, symbol): return 1.0850, 1.0851, 1.0
        def get_positions(self, symbol=None): return []
        def get_open_positions_count(self, symbol=None): return 0
        def cancel_order(self, order_id): return True
        def close_position(self, position_id): return True
        def close_all_positions(self): return 0

        def execute_order(self, symbol, action, lot_size, sl, tp, client_order_id=None, comment=""):
            # Simulate order processed by broker backend, but socket drops before response is sent back
            self._executed[client_order_id] = OrderResult(
                success=True, order_id="MT5-RECOVERED-77", client_order_id=client_order_id,
                symbol=symbol, action=action.value, lot_size=lot_size, price=1.0851, sl=sl, tp=tp,
                status="FILLED", timestamp_utc=datetime.now(timezone.utc)
            )
            raise ConnectionResetError("Socket connection dropped during packet transmission")

        def get_order(self, client_order_id=None, broker_order_id=None):
            return self._executed.get(client_order_id)

    dropping_broker = DroppingBroker()
    dispatcher = OrderDispatcher(oms=oms, broker=dropping_broker)

    signal = TradeSignal("EURUSD", SignalAction.BUY, 1.0850, 1.0800, 1.0950, "Network Drop Test")
    result = dispatcher.dispatch(signal, lot_size=0.3)

    # Must recover without blind-resending a duplicate trade
    assert result.success is True
    assert result.order_id == "MT5-RECOVERED-77"
    order = repo.get_order_by_client_id(result.client_order_id)
    assert order is not None
    assert order.state == OrderState.FILLED


# --- SCENARIO 3: DATABASE FAILURE / DISK CORRUPT ---
def test_scenario_3_database_failure_fail_closed_no_new_risk(chaos_env):
    db_path = chaos_env["db_path"]
    repo = OMSRepository(db_path=db_path)
    oms = OMSService(repo)
    broker = MockBrokerAdapter(initial_balance=100_000.0)
    broker.connect()
    dispatcher = OrderDispatcher(oms=oms, broker=broker)

    # Corrupt both DB and WAL journal intentionally
    with open(db_path, "wb") as f:
        f.write(b"CORRUPTED_SQLITE_GARBAGE_HEADER_12345")
    wal_path = f"{db_path}-wal"
    if os.path.exists(wal_path):
        with open(wal_path, "wb") as f:
            f.write(b"CORRUPTED_WAL_GARBAGE")

    signal = TradeSignal("EURUSD", SignalAction.BUY, 1.0850, 1.0800, 1.0950, "Corrupt DB Test")

    # When DB is corrupted, OMS cannot persist -> Fail-closed NO NEW RISK, broker is NOT called
    result = dispatcher.dispatch(signal, lot_size=0.1)
    assert result.success is False
    assert result.status == "REJECTED"
    assert "Database failure" in (result.error_message or "")
    assert broker.get_open_positions_count() == 0  # Zero positions opened at broker!


# --- SCENARIO 4: MARKET DATA QUOTE FREEZE ---
def test_scenario_4_market_data_freeze_blocked_by_freshness_gate():
    gate = FreshnessGate(max_stale_seconds=3.0, max_spread_pips=2.5)
    now = datetime.now(timezone.utc)

    # Market data freeze: quotes stopped arriving 12 seconds ago
    frozen_quote = Quote(
        symbol="EURUSD",
        bid=1.0850,
        ask=1.0851,
        spread_pips=1.0,
        timestamp_utc=now - timedelta(seconds=12.0)
    )

    validation = gate.validate_quote(frozen_quote, now_utc=now)
    assert validation.is_valid is False
    assert "Stale quote rejected" in validation.reason


# --- SCENARIO 5: SPLIT BRAIN PREVENTION ---
def test_scenario_5_split_brain_prevented_by_leader_lock(chaos_env):
    lock_file = chaos_env["leader_lock_file"]
    primary_engine = LeaderLock(lock_path=lock_file)
    secondary_engine = LeaderLock(lock_path=lock_file)

    # Primary engine starts and acquires lock
    assert primary_engine.acquire() is True

    # Secondary engine accidentally launched by operator -> MUST BE BLOCKED
    assert secondary_engine.acquire() is False

    primary_engine.release()


# --- SCENARIO 6: BAD CONFIGURATION DEPLOYMENT ---
def test_scenario_6_bad_config_deployment_rejected_by_validator():
    validator = ConfigValidator(max_allowed_single_risk_pct=1.0, firm_ceiling_daily_dd_pct=5.0)
    rules = PropFirmRules(max_daily_loss_pct=5.0)

    # Bad config #1: Single trade risk = 5.0%
    res1 = validator.validate(rules, risk_per_trade_pct=5.0, max_concurrent_positions=1)
    assert res1.is_valid is False
    assert any("Dangerous single-trade risk" in e for e in res1.errors)

    # Bad config #2: 4 positions * 1.5% = 6% > 5% daily limit
    res2 = validator.validate(rules, risk_per_trade_pct=1.5, max_concurrent_positions=4)
    assert res2.is_valid is False
    assert any("Worst-Case Exposure Breach" in e for e in res2.errors)


# --- SCENARIO 7: CLOCK DRIFT ANOMALY ---
def test_scenario_7_clock_drift_anomaly_detected():
    provider = ClockProvider("Europe/Nicosia")
    now_broker = provider.broker_time()

    # Simulate VPS clock jumping +10 minutes ahead (NTP desync)
    skewed_time = now_broker + timedelta(minutes=10)

    ok, drift, msg = provider.validate_clock_drift(skewed_time, max_drift_seconds=2.0)
    assert ok is False
    assert drift >= 590.0
    assert "Clock drift anomaly detected" in msg
