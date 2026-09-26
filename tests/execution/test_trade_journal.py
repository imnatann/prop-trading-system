"""Tests for TradeJournal (dedicated trade recording ledger)."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from src.execution.trade_journal import TradeJournal


def test_trade_journal_records_open_and_close(tmp_path):
    journal = TradeJournal(tmp_path)

    # 1. Record open
    open_entry = journal.record_open(
        trade_id="10001",
        symbol="EURUSD",
        side="BUY",
        volume=0.01,
        requested_price=1.08500,
        filled_price=1.08502,
        slippage_pips=0.2,
        sl=1.08000,
        tp=1.09000,
        mode="TEST",
        comment="test smoke open",
    )
    assert open_entry["trade_id"] == "10001"
    assert open_entry["action"] == "OPEN"

    assert journal.jsonl_path.exists()
    assert journal.csv_path.exists()

    # Verify JSONL
    lines = journal.jsonl_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["trade_id"] == "10001"
    assert data["action"] == "OPEN"
    assert data["volume"] == 0.01

    # Verify CSV
    with journal.csv_path.open("r", encoding="utf-8") as f:
        reader = list(csv.DictReader(f))
        assert len(reader) == 1
        assert reader[0]["trade_id"] == "10001"
        assert reader[0]["action"] == "OPEN"
        assert float(reader[0]["filled_price"]) == 1.08502

    # 2. Record close
    close_entry = journal.record_close(
        trade_id="10001",
        symbol="EURUSD",
        side="BUY",
        volume=0.01,
        entry_price=1.08502,
        exit_price=1.08552,
        gross_pnl=5.00,
        net_pnl=4.80,
        commission=-0.20,
        swap=0.0,
        hold_duration_sec=120.0,
        mode="TEST",
        comment="test smoke close",
    )
    assert close_entry["trade_id"] == "10001"
    assert close_entry["action"] == "CLOSE"

    lines = journal.jsonl_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["action"] == "CLOSE"

    with journal.csv_path.open("r", encoding="utf-8") as f:
        reader = list(csv.DictReader(f))
        assert len(reader) == 2
        assert reader[1]["action"] == "CLOSE"
        assert float(reader[1]["net_pnl"]) == 4.80

    # 3. List trades
    trades = journal.list_trades(limit=10)
    assert len(trades) == 2
    # newest first
    assert trades[0]["action"] == "CLOSE"
    assert trades[1]["action"] == "OPEN"


def test_view_trades_cli(tmp_path, capsys):
    from scripts import view_trades as cli

    journal = TradeJournal(tmp_path)
    journal.record_open(
        trade_id="999",
        symbol="GBPUSD",
        side="SELL",
        volume=0.05,
        requested_price=1.25000,
        filled_price=1.25000,
        mode="MOCK",
    )

    code = cli.run(["--trades-dir", str(tmp_path)])
    assert code == 0
    out = capsys.readouterr().out
    assert "GBPUSD" in out
    assert "SELL" in out
    assert "0.05" in out
