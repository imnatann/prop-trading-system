"""Tests for scripts/fundingpips_bot.py (Automated Trading Bot Daemon)."""
from __future__ import annotations

import json
from pathlib import Path

from scripts import fundingpips_bot as bot_cli
from src.execution.fundingpips_mt5 import FundingPipsMT5Adapter
from tests.execution.fake_mt5 import FakeAccount, fakes_env, make_fake


def test_bot_dry_run_scans_market_without_order(tmp_path, capsys):
    """Bot scans bars and runs strategy in dry-run mode without sending orders."""
    trades_dir = tmp_path / "trades"
    code = bot_cli.run([
        "--symbol", "EURUSD",
        "--mock",
        "--poll-interval", "0.01",
        "--max-iterations", "2",
        "--trades-dir", str(trades_dir),
    ])
    assert code == 0
    out = capsys.readouterr().out
    assert "FundingPips Automated Trading Bot" in out
    assert "PAPER MONITOR (DRY RUN)" in out
    assert "Balance:" in out
    # In dry-run, no order is sent
    assert not (trades_dir / "trades.jsonl").exists()


def test_bot_executes_order_with_allow_order(tmp_path, capsys, monkeypatch):
    """When both opt-ins are provided, bot places live order on signal."""
    monkeypatch.setenv("FUNDINGPIPS_ALLOW_TRADING", "true")
    trades_dir = tmp_path / "trades"

    code = bot_cli.run([
        "--symbol", "EURUSD",
        "--mock",
        "--allow-order",
        "--poll-interval", "0.01",
        "--max-iterations", "3",
        "--trades-dir", str(trades_dir),
    ])
    assert code == 0
    out = capsys.readouterr().out
    assert "LIVE AUTONOMOUS TRADING" in out
    assert "Bot loop started." in out
    # Verify trade journal recorded the automated trade
    assert (trades_dir / "trades.jsonl").exists()
    assert (trades_dir / "trades.csv").exists()
    records = (trades_dir / "trades.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(records) >= 1
    data = json.loads(records[0])
    assert data["symbol"] == "EURUSD"
    assert data["mode"] == "MOCK"


def test_bot_halts_on_daily_floor_breach(tmp_path, capsys, monkeypatch):
    """If equity drops to or below daily floor, bot halts immediately."""
    monkeypatch.setenv("FUNDINGPIPS_ALLOW_TRADING", "true")
    trades_dir = tmp_path / "trades"

    # Monkeypatch build_adapter to simulate an account already at breach level
    fake = make_fake(account=FakeAccount(balance=10000.0, equity=9400.0))  # 6% loss > 5% floor
    adapter = FundingPipsMT5Adapter.from_env(env=fakes_env(), mt5_module=fake, allow_order=True)
    monkeypatch.setattr(bot_cli, "build_adapter", lambda **kw: adapter)

    code = bot_cli.run([
        "--symbol", "EURUSD",
        "--mock",
        "--allow-order",
        "--poll-interval", "0.01",
        "--max-iterations", "1",
        "--trades-dir", str(trades_dir),
    ])
    assert code == 5  # Emergency halt code
    out = capsys.readouterr().out
    assert "EMERGENCY RISK BREACH" in out
