import os
import tempfile
import pytest

from services.canary_runner import CanarySoakRunner


@pytest.fixture
def temp_env():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "canary_test.db")
        hb_dir = os.path.join(tmpdir, "heartbeats")
        yield db_path, hb_dir


def test_canary_runner_all_checks_pass(temp_env):
    db_path, hb_dir = temp_env
    runner = CanarySoakRunner(db_path=db_path, heartbeat_dir=hb_dir)

    report = runner.run_all_canary_checks()

    assert report.total_steps == 6
    assert report.passed_steps == 6
    assert report.failed_steps == 0
    assert report.is_fully_healthy is True

    for step in report.step_results:
        assert step["status"] == "PASS"
