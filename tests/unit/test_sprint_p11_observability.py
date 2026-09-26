import os
import tempfile
import pytest

from src.observability.metrics import MetricsCollector
from src.observability.audit import AuditLogger
from src.observability.alerts import AlertManager
from src.oms.repository import OMSRepository


@pytest.fixture
def temp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    yield db_path
    for ext in ["", "-wal", "-shm"]:
        p = f"{db_path}{ext}"
        if os.path.exists(p):
            os.remove(p)


def test_metrics_collector():
    collector = MetricsCollector()

    # Record 3 executions
    collector.record_execution("QP-1", "EURUSD", "BUY", requested_price=1.0850, executed_price=1.0851, latency_ms=45.0, success=True)
    collector.record_execution("QP-2", "EURUSD", "BUY", requested_price=1.0850, executed_price=1.0853, latency_ms=65.0, success=True)
    collector.record_execution("QP-3", "EURUSD", "SELL", requested_price=1.0850, executed_price=0.0, latency_ms=120.0, success=False)

    summary = collector.get_summary()
    assert summary["total_executions"] == 3
    assert summary["success_rate_pct"] == 66.67
    assert summary["mean_latency_ms"] == pytest.approx(76.67, 0.01)
    assert summary["mean_slippage_pips"] == pytest.approx(2.0, 0.01)  # (1.0 + 3.0)/2


def test_audit_logger(temp_db):
    repo = OMSRepository(db_path=temp_db)
    logger = AuditLogger(repo=repo)

    rec = logger.log(
        category="RISK",
        severity="INFO",
        action="EVALUATION_APPROVED",
        details={"symbol": "EURUSD", "lot": 0.5}
    )
    assert rec.category == "RISK"

    # Query directly from DB to verify persistence
    with repo._get_connection() as conn:
        cursor = conn.execute("SELECT * FROM system_events")
        rows = cursor.fetchall()
        assert len(rows) == 1
        assert rows[0]["event_type"] == "RISK"
        assert rows[0]["severity"] == "INFO"


def test_alert_manager_debounce():
    alerts_received = []

    def mock_handler(topic, severity, message):
        alerts_received.append((topic, severity, message))

    mgr = AlertManager(debounce_seconds=10.0)
    mgr.register_handler(mock_handler)

    # First alert sent
    sent1 = mgr.trigger_alert("DRAWDOWN_WARNING", "WARNING", "Equity reached 3% DD")
    assert sent1 is True
    assert len(alerts_received) == 1

    # Second identical topic alert immediate -> suppressed
    sent2 = mgr.trigger_alert("DRAWDOWN_WARNING", "WARNING", "Equity reached 3.1% DD")
    assert sent2 is False
    assert len(alerts_received) == 1

    # Different topic alert -> delivered immediately
    sent3 = mgr.trigger_alert("BROKER_DISCONNECT", "CRITICAL", "Lost connection")
    assert sent3 is True
    assert len(alerts_received) == 2
