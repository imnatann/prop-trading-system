import os
import tempfile
import pytest

from scripts.verify_mt5_connection import verify_connection
from scripts.test_order_lifecycle import run_lifecycle_test


@pytest.fixture
def temp_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    yield db_path
    for ext in ["", "-wal", "-shm"]:
        p = f"{db_path}{ext}"
        if os.path.exists(p):
            os.remove(p)


def test_mt5_preflight_connection_check():
    ok = verify_connection(use_mock_fallback=True)
    assert ok is True


def test_order_lifecycle_flow(temp_db):
    ok = run_lifecycle_test(db_path=temp_db)
    assert ok is True
