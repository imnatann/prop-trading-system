import os
import tempfile
import sqlite3
import pytest
from datetime import datetime, timedelta, timezone

from config.prop_rules import PropFirmRules
from config.validator import ConfigValidator
from src.broker.base import PositionInfo
from src.broker.clock_provider import ClockProvider
from src.execution.broker_adapter import MockBrokerAdapter
from src.oms.repository import OMSRepository
from src.safety.leader_lock import LeaderLock
from src.safety.startup_guard import StartupGuard
from src.storage.health import DatabaseHealth
from src.storage.migration import DatabaseMigration
from scripts.backup_db import run_backup


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


def test_database_migration_and_versioning(temp_dir):
    db_path = os.path.join(temp_dir, "test_mig.db")
    mig = DatabaseMigration(db_path=db_path)

    # Initially v0 -> auto-inits to v1
    ok, msg = mig.check_compatibility(expected_version=1)
    assert ok is True
    assert mig.get_current_version() == 1

    # Upgrade to v2
    mig.apply_version(2, "Add strategy versioning table", "CREATE TABLE test_v2 (id INT);")
    assert mig.get_current_version() == 2

    # Expecting v1 on v2 DB should report incompatibility
    ok2, msg2 = mig.check_compatibility(expected_version=1)
    assert ok2 is False
    assert "Backward compatibility issue" in msg2


def test_database_health_checks(temp_dir):
    db_path = os.path.join(temp_dir, "test_health.db")
    repo = OMSRepository(db_path=db_path)
    health = DatabaseHealth(db_path=db_path)

    res = health.full_health_check()
    assert res["is_healthy"] is True
    assert res["integrity_ok"] is True
    assert res["disk_ok"] is True
    assert res["lock_ok"] is True


def test_online_hot_backup_and_integrity(temp_dir):
    db_path = os.path.join(temp_dir, "source.db")
    backup_dir = os.path.join(temp_dir, "backups")

    # Create dummy table & data in source
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE orders (id INT, sym TEXT);")
    conn.execute("INSERT INTO orders VALUES (1, 'EURUSD');")
    conn.commit()
    conn.close()

    success = run_backup(db_path=db_path, backup_dir=backup_dir)
    assert success is True

    # Verify backup exists and is readable
    backups = list(os.listdir(backup_dir))
    assert len(backups) == 1
    b_path = os.path.join(backup_dir, backups[0])
    b_conn = sqlite3.connect(b_path)
    cursor = b_conn.execute("SELECT * FROM orders")
    rows = cursor.fetchall()
    b_conn.close()
    assert len(rows) == 1
    assert rows[0][1] == "EURUSD"


def test_clock_provider_and_drift():
    provider = ClockProvider("Europe/Nicosia")
    assert provider.system_time().tzinfo == timezone.utc
    assert provider.broker_time().tzinfo is not None

    session = provider.market_session(now_utc=datetime(2026, 9, 20, 14, 0, tzinfo=timezone.utc))
    assert session == "LONDON_NY_OVERLAP"

    # Drift test
    now = provider.broker_time()
    # Acceptable tiny drift (0.1s)
    ok, drift, _ = provider.validate_clock_drift(now + timedelta(seconds=0.1), max_drift_seconds=1.0)
    assert ok is True

    # Excessive drift (5s)
    ok_drift, drift_val, msg = provider.validate_clock_drift(now + timedelta(seconds=5.0), max_drift_seconds=1.0)
    assert ok_drift is False
    assert "Clock drift anomaly detected" in msg


def test_split_brain_leader_lock(temp_dir):
    lock_file = os.path.join(temp_dir, "engine.lock")
    lock1 = LeaderLock(lock_path=lock_file)
    lock2 = LeaderLock(lock_path=lock_file)

    # First instance acquires lock
    assert lock1.acquire() is True

    # Second instance tries to acquire while first is active -> BLOCKED (Split-Brain prevented)
    assert lock2.acquire() is False

    # First instance releases lock
    assert lock1.release() is True

    # Now second instance can acquire
    assert lock2.acquire() is True
    lock2.release()


def test_config_validator_single_and_worst_case_exposure():
    validator = ConfigValidator(max_allowed_single_risk_pct=1.0, firm_ceiling_daily_dd_pct=5.0)
    rules = PropFirmRules(max_daily_loss_pct=5.0, kill_switch_daily_pct=4.5)

    # 1. Safe Configuration: 0.5% risk * 3 positions = 1.5% worst case < 5.0% daily DD
    res_safe = validator.validate(rules, risk_per_trade_pct=0.5, max_concurrent_positions=3)
    assert res_safe.is_valid is True
    assert len(res_safe.errors) == 0

    # 2. Dangerous Single Risk: 5.0% risk per trade > 1.0% allowed
    res_danger = validator.validate(rules, risk_per_trade_pct=5.0, max_concurrent_positions=1)
    assert res_danger.is_valid is False
    assert any("Dangerous single-trade risk" in e for e in res_danger.errors)

    # 3. Worst-Case Exposure Breach: 2.0% risk * 3 positions = 6.0% > 5.0% daily DD
    res_worst = validator.validate(rules, risk_per_trade_pct=2.0, max_concurrent_positions=3)
    assert res_worst.is_valid is False
    assert any("Worst-Case Exposure Breach" in e for e in res_worst.errors)


def test_startup_guard_naked_position_rejection_and_clean_startup(temp_dir):
    db_path = os.path.join(temp_dir, "trading.db")
    repo = OMSRepository(db_path=db_path)
    broker = MockBrokerAdapter(initial_balance=100_000.0)
    broker.connect()
    rules = PropFirmRules()

    guard = StartupGuard(
        broker=broker,
        repo=repo,
        rules=rules,
        lock_file_path=os.path.join(temp_dir, "halt.lock"),
        leader_lock=LeaderLock(os.path.join(temp_dir, "engine.lock"))
    )

    # 1. Clean Startup without positions -> Allowed
    decision = guard.evaluate_startup()
    assert decision.allowed is True
    guard.leader_lock.release()

    # 2. Add Naked Position on broker (no Stop Loss, sl=0.0)
    broker.positions.append(PositionInfo(
        position_id="NAKED-1", client_order_id="QP-1", symbol="EURUSD", action="BUY",
        volume=1.0, entry_price=1.0850, sl=0.0, tp=1.0950, current_price=1.0850, profit=0.0,
        open_time=datetime.now(timezone.utc)
    ))

    decision_naked = guard.evaluate_startup()
    assert decision_naked.allowed is False
    assert "NAKED POSITION DETECTED" in decision_naked.reason
