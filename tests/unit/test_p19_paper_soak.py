import os
import tempfile
import pytest

from services.paper_soak import PaperSoakHarness


@pytest.fixture
def temp_soak_env():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "soak_test.db")
        hb_dir = os.path.join(tmpdir, "heartbeats")
        lock_file = os.path.join(tmpdir, "soak.leader.lock")
        yield db_path, hb_dir, lock_file


def test_paper_soak_harness_lifecycle(temp_soak_env):
    db_path, hb_dir, lock_file = temp_soak_env
    harness = PaperSoakHarness(
        db_path=db_path,
        heartbeat_dir=hb_dir,
        leader_lock_file=lock_file,
        initial_balance=100_000.0
    )

    # 1. Test startup
    ok = harness.startup()
    assert ok is True

    # 2. Test individual failure drills
    res_stale = harness.run_failure_drill("STALE_DATA_INJECTION")
    assert res_stale.passed is True

    res_news = harness.run_failure_drill("NEWS_BLACKOUT_INJECTION")
    assert res_news.passed is True

    res_clean = harness.run_failure_drill("REVERSAL_RECONCILIATION_DRILL")
    assert res_clean.passed is True

    # 3. Test multi-cycle soak execution
    report = harness.run_soak(
        total_cycles=30,
        health_audit_interval=10,
        drill_interval=10
    )

    assert report.total_cycles >= 30
    assert report.health_audits_passed >= 3
    assert report.drills_passed == report.drills_run
    assert report.reconciliations_clean == report.reconciliations_run
    assert report.is_fully_healthy is True
    assert "Paper Soak Completed" in report.summary
