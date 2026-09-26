"""CLI-level safety tests.

Verifies the operator-facing guarantees end to end:
  * the connection check cannot place an order
  * the spread recorder cannot place an order
  * the smoke utility needs BOTH opt-ins, reports a dry run otherwise
  * the FundingPips-Trial server is required

No test connects to a real terminal. The adapter is monkeypatched with the fake
gateway so the CLI logic itself is what is under test.
"""
from __future__ import annotations

import os
import pytest

from src.execution.fundingpips_mt5 import FundingPipsMT5Adapter
from src.execution.redaction import clear_registered_secrets
from tests.execution.fake_mt5 import FakeAccount, FakeMetaTrader5, fakes_env, make_fake


@pytest.fixture(autouse=True)
def _clean_secrets():
    clear_registered_secrets()
    yield
    clear_registered_secrets()


@pytest.fixture
def patched_adapter(monkeypatch):
    """Force every CLI to build a fake-gateway adapter and share one instance."""
    holder = {}

    def factory(allow_order: bool = False, max_spread_pips=None, mock: bool = False, **kwargs):
        fake = make_fake()
        allow_tr = os.environ.get("FUNDINGPIPS_ALLOW_TRADING", "false")
        adapter = FundingPipsMT5Adapter.from_env(
            env=fakes_env(FUNDINGPIPS_ALLOW_TRADING=allow_tr), mt5_module=fake, allow_order=allow_order,
            max_spread_pips=max_spread_pips,
        )
        holder["adapter"] = adapter
        holder["fake"] = fake
        return adapter

    monkeypatch.setattr("scripts.fundingpips_cli.build_adapter", factory)
    # each CLI imports build_adapter into its own module namespace
    for module in ("scripts.fundingpips_connection_check",
                   "scripts.fundingpips_symbol_probe",
                   "scripts.fundingpips_record_spread",
                   "scripts.fundingpips_smoke_order"):
        monkeypatch.setattr(module + ".build_adapter", factory, raising=False)
    return holder


# ------------------------------------------- case 8: connection check cannot order
def test_connection_check_places_no_order(patched_adapter, capsys):
    from scripts import fundingpips_connection_check as cli

    code = cli.run()
    assert code == 0
    assert patched_adapter["fake"].call_count("order_send") == 0


def test_connection_check_builds_adapter_with_trading_off(patched_adapter):
    from scripts import fundingpips_connection_check as cli

    cli.run()
    adapter = patched_adapter["adapter"]
    assert adapter.trading_enabled is False
    assert adapter.can_trade is False


def test_connection_check_masks_the_account(patched_adapter, capsys):
    from scripts import fundingpips_connection_check as cli

    cli.run()
    output = capsys.readouterr().out
    assert "****5678" in output
    assert "12345678" not in output


def test_connection_check_shows_symbol_metadata(patched_adapter, capsys):
    from scripts import fundingpips_connection_check as cli

    cli.run()
    output = capsys.readouterr().out
    assert "EURUSD" in output
    assert "FundingPips-Trial" in output
    assert "Read-only check complete" in output


def test_connection_check_announces_trading_disabled(patched_adapter, capsys):
    from scripts import fundingpips_connection_check as cli

    cli.run()
    output = capsys.readouterr().out
    assert "Trading enabled" in output
    assert "NO" in output


# ------------------------------------------- case 9: spread recorder cannot order
def test_spread_recorder_places_no_order(patched_adapter):
    from scripts import fundingpips_record_spread as cli

    code = cli.run(["--symbol", "EURUSD", "--duration", "0", "--no-save"])
    assert code == 0
    assert patched_adapter["fake"].call_count("order_send") == 0


def test_spread_recorder_records_samples(tmp_path, patched_adapter):
    from scripts import fundingpips_record_spread as cli

    code = cli.run([
        "--symbol", "EURUSD", "--duration", "0.3", "--interval", "0.01",
        "--output-dir", str(tmp_path),
    ])
    assert code == 0
    assert patched_adapter["fake"].call_count("order_send") == 0
    assert patched_adapter["fake"].call_count("symbol_info_tick") > 0


def test_spread_recorder_never_imports_strategy():
    import inspect

    from scripts import fundingpips_record_spread as cli

    source = inspect.getsource(cli)
    assert "alpha_v2" not in source
    assert "strategy" not in source.replace("imports NO strategy", "")


# ------------------------------------------- case 20: smoke order gates
def test_smoke_order_dry_run_without_flags(monkeypatch, capsys):
    from scripts import fundingpips_smoke_order as cli

    fake = make_fake()
    adapter = FundingPipsMT5Adapter.from_env(
        env=fakes_env(), mt5_module=fake, allow_order=False
    )
    monkeypatch.setattr(cli, "build_adapter", lambda **kw: adapter)

    code = cli.run(["--symbol", "EURUSD", "--volume", "0.01"])
    assert code == 0
    assert fake.call_count("order_send") == 0
    output = capsys.readouterr().out
    assert "PREFLIGHT DRY RUN" in output
    assert "Order NOT sent" in output


def _fake_off():
    adapter = FundingPipsMT5Adapter.from_env(
        env=fakes_env(), mt5_module=make_fake(), allow_order=False
    )
    return adapter


def test_smoke_order_requires_both_optins(monkeypatch, capsys):
    """With the env flag true but no CLI flag, still no order."""
    from scripts import fundingpips_smoke_order as cli

    fake = make_fake()
    env = fakes_env(FUNDINGPIPS_ALLOW_TRADING="true")
    adapter = FundingPipsMT5Adapter.from_env(env=env, mt5_module=fake, allow_order=False)
    monkeypatch.setattr(cli, "build_adapter", lambda **kw: adapter)

    code = cli.run(["--symbol", "EURUSD", "--volume", "0.01"])
    assert code == 0
    assert fake.call_count("order_send") == 0
    output = capsys.readouterr().out
    assert "--allow-order" in output


def test_smoke_order_places_order_with_both_optins(monkeypatch, capsys, isolated_evidence):
    """Only when ALL gates are open does a single order go out.

    There are now three gates, not two:
      1. FUNDINGPIPS_ALLOW_TRADING=true   (configuration)
      2. --allow-order                    (explicit invocation flag)
      3. stage-1 evidence                 (measured spread, resolved symbol)

    Gate 3 is evidence-based rather than declarative, so a test that only sets
    the two declarative opt-ins must ALSO acknowledge the missing evidence. That
    acknowledgement is exactly what --skip-readiness-check means in production.
    """
    from scripts import fundingpips_smoke_order as cli

    fake = make_fake()
    env = fakes_env(FUNDINGPIPS_ALLOW_TRADING="true")
    adapter = FundingPipsMT5Adapter.from_env(env=env, mt5_module=fake, allow_order=True)
    monkeypatch.setattr(cli, "build_adapter", lambda **kw: adapter)

    code = cli.run(["--symbol", "EURUSD", "--volume", "0.01", "--allow-order",
                    "--hold-seconds", "0", "--keep-open",
                    "--skip-readiness-check"])
    assert code == 0
    assert fake.call_count("order_send") == 1, "exactly one smoke order, never a loop"


@pytest.fixture
def isolated_evidence(tmp_path, monkeypatch):
    """Point EVERY stage-1 evidence path at a throwaway directory.

    Without this the smoke-order tests write a real connection_evidence.json and
    stage1_readiness.json into data/execution, which the live readiness CLI then
    reads as if a genuine MT5 login had occurred. That is fabricated evidence,
    and it happened once before this fixture existed.
    """
    from src.execution import readiness as readiness_mod

    d = tmp_path / "evidence"
    (d / "canonical").mkdir(parents=True)
    monkeypatch.setattr(readiness_mod, "READINESS_DIR", d, raising=False)
    monkeypatch.setattr(readiness_mod, "READINESS_PATH",
                        d / "stage1_readiness.json", raising=False)
    monkeypatch.setattr(readiness_mod, "CONNECTION_PATH",
                        d / "connection_evidence.json", raising=False)
    # save_readiness / save_connection_evidence default their path from the
    # module-level constants, so patch those too or a default call still lands
    # in the real directory.
    return d


@pytest.fixture(autouse=True)
def _never_write_real_evidence(monkeypatch, tmp_path_factory):
    """Belt and braces: no test in this file may touch the real evidence dir."""
    from src.execution import readiness as rm
    from src.execution import execution_profile as ep
    d = tmp_path_factory.mktemp("evidence")
    monkeypatch.setattr(rm, "READINESS_DIR", d, raising=False)
    monkeypatch.setattr(rm, "READINESS_PATH", d / "r.json", raising=False)
    monkeypatch.setattr(rm, "CONNECTION_PATH", d / "c.json", raising=False)
    monkeypatch.setattr(ep, "PROFILE_DIR", d, raising=False)
    monkeypatch.setattr(ep, "PROFILE_PATH", d / "execution_profile.json",
                        raising=False)
    yield d


def test_smoke_order_refuses_when_stage1_evidence_is_missing(monkeypatch, capsys, isolated_evidence):
    """The third gate must BLOCK even when both opt-ins are supplied.

    An unmeasured spread makes the cost attribution this tool prints afterwards
    meaningless, so missing evidence is a refusal rather than a warning.
    """
    from scripts import fundingpips_smoke_order as cli

    fake = make_fake()
    env = fakes_env(FUNDINGPIPS_ALLOW_TRADING="true")
    adapter = FundingPipsMT5Adapter.from_env(env=env, mt5_module=fake, allow_order=True)
    monkeypatch.setattr(cli, "build_adapter", lambda **kw: adapter)

    code = cli.run(["--symbol", "EURUSD", "--volume", "0.01", "--allow-order",
                    "--hold-seconds", "0", "--keep-open"])
    assert code == 4, "must refuse with the precheck exit code"
    assert fake.call_count("order_send") == 0, "NOTHING may be sent"
    out = capsys.readouterr().out
    assert "REFUSING TO SEND" in out
    assert "fundingpips_record_spread" in out, "must tell the operator what is missing"


def test_smoke_order_rejects_unaligned_volume_without_sending(monkeypatch, capsys):
    from scripts import fundingpips_smoke_order as cli

    fake = make_fake()
    env = fakes_env(FUNDINGPIPS_ALLOW_TRADING="true")
    adapter = FundingPipsMT5Adapter.from_env(env=env, mt5_module=fake, allow_order=True)
    monkeypatch.setattr(cli, "build_adapter", lambda **kw: adapter)

    cli.run(["--symbol", "EURUSD", "--volume", "0.017", "--allow-order", "--keep-open"])
    assert fake.call_count("order_send") == 0
    assert "REFUSING" in capsys.readouterr().out


def test_smoke_order_rejects_sub_minimum_volume_without_sending(monkeypatch, capsys):
    from scripts import fundingpips_smoke_order as cli

    fake = make_fake()
    env = fakes_env(FUNDINGPIPS_ALLOW_TRADING="true")
    adapter = FundingPipsMT5Adapter.from_env(env=env, mt5_module=fake, allow_order=True)
    monkeypatch.setattr(cli, "build_adapter", lambda **kw: adapter)

    cli.run(["--symbol", "EURUSD", "--volume", "0.001", "--allow-order", "--keep-open"])
    assert fake.call_count("order_send") == 0
    assert "REFUSING" in capsys.readouterr().out


def test_smoke_order_respects_the_anti_scalping_default_hold():
    """The default hold must come from the FundingPips rule, not be invented."""
    import argparse
    import inspect

    from scripts import fundingpips_smoke_order as cli

    source = inspect.getsource(cli)
    assert "FUNDING_PIPS_2STEP.min_trade_duration_seconds" in source


# ------------------------------------------- wrong server rejected at the CLI
def test_adapter_refuses_wrong_server():
    from src.execution.errors import UnexpectedServerError

    adapter = FundingPipsMT5Adapter.from_env(
        env=fakes_env(server="OtherBroker-Demo"), mt5_module=make_fake()
    )
    with pytest.raises(UnexpectedServerError):
        adapter.connect()


def test_account_mismatch_rejected_at_connect():
    from src.execution.errors import AccountMismatchError

    adapter = FundingPipsMT5Adapter.from_env(
        env=fakes_env(login="11111111"), mt5_module=make_fake(account=FakeAccount(login=22222222))
    )
    with pytest.raises(AccountMismatchError):
        adapter.connect()


# ------------------------------------------- missing credentials exit cleanly
def test_connection_check_missing_credentials_exit_code(monkeypatch, capsys):
    from scripts import fundingpips_connection_check as cli
    from scripts.fundingpips_cli import EXIT_MISSING_CREDENTIALS

    monkeypatch.setattr(cli, "build_adapter", lambda **kw: _no_cred_adapter())
    code = cli.run()
    assert code == EXIT_MISSING_CREDENTIALS
    output = capsys.readouterr().out
    assert "MISSING CREDENTIALS" in output
    assert "FUNDINGPIPS_MT5_LOGIN" in output


def _no_cred_adapter():
    return FundingPipsMT5Adapter.from_env(env={}, require_credentials=False, mt5_module=make_fake())


def test_symbol_probe_masks_account(patched_adapter, capsys):
    from scripts import fundingpips_symbol_probe as cli

    cli.run(["EURUSD"])
    output = capsys.readouterr().out
    assert "12345678" not in output.replace("****5678", "")


def test_full_mock_stage1_to_smoke_lifecycle(tmp_path, capsys, monkeypatch):
    """End-to-end verification of the Stage 1 to smoke order sequence with --mock.

    This ensures operators can test and verify the entire CLI toolchain locally
    (e.g. on macOS/Linux) using simulated MT5 before running live on Windows.
    All evidence artefacts are strictly routed to tmp_path.
    """
    import json
    from scripts import fundingpips_connection_check as conn_cli
    from scripts import fundingpips_symbol_probe as probe_cli
    from scripts import fundingpips_record_spread as spread_cli
    from scripts import fundingpips_readiness as readiness_cli
    from scripts import fundingpips_smoke_order as smoke_cli

    conn_path = tmp_path / "connection_evidence.json"
    profile_path = tmp_path / "execution_profile.json"
    canonical_dir = tmp_path / "canonical"
    readiness_path = tmp_path / "stage1_readiness.json"

    # 1. Connection check with --mock
    ret = conn_cli.run(["--mock", "--evidence-path", str(conn_path)])
    assert ret == 0
    assert conn_path.exists()
    conn_data = json.loads(conn_path.read_text(encoding="utf-8"))
    assert conn_data.get("logged_in") is True

    # 2. Symbol probe with --mock and --save-profile
    ret = probe_cli.run(["EURUSD", "--mock", "--save-profile", "--save-path", str(profile_path)])
    assert ret == 0
    assert profile_path.exists()
    profile_data = json.loads(profile_path.read_text(encoding="utf-8"))
    assert profile_data.get("canonical_symbol") == "EURUSD"

    # 3. Record spread with --mock
    ret = spread_cli.run(["--symbol", "EURUSD", "--mock", "--duration", "0.05",
                          "--interval", "0.01", "--output-dir", str(canonical_dir)])
    assert ret == 0
    assert canonical_dir.exists()

    # Seed 250 valid samples so MIN_SPREAD_SAMPLES (>=200) threshold is satisfied
    spread_file = next(canonical_dir.glob("*.jsonl"))
    lines = [json.dumps({"symbol": "EURUSD", "bid": 1.08500, "ask": 1.08512, "spread_pips": 1.2, "timestamp_utc": "2026-09-25T12:00:00Z"}) + "\n" for _ in range(250)]
    spread_file.write_text("".join(lines), encoding="utf-8")

    # Set FUNDINGPIPS_ALLOW_TRADING=true in environment for readiness & smoke order
    monkeypatch.setenv("FUNDINGPIPS_ALLOW_TRADING", "true")

    # 4. Stage-1 readiness audit
    ret = readiness_cli.run(["--evidence-dir", str(tmp_path), "--save", "--save-path", str(readiness_path)])
    assert ret == 0
    assert readiness_path.exists()
    readiness_data = json.loads(readiness_path.read_text(encoding="utf-8"))
    assert readiness_data.get("ready_for_smoke_order") is True

    # 5. Smoke order with --mock and --allow-order
    ret = smoke_cli.run([
        "--symbol", "EURUSD",
        "--mock",
        "--allow-order",
        "--hold-seconds", "0",
        "--keep-open",
        "--evidence-dir", str(tmp_path),
        "--readiness-path", str(readiness_path),
    ])
    assert ret == 0
    out = capsys.readouterr().out
    assert "Smoke sequence complete." in out
    assert "TRADE_RETCODE_DONE" in out

    # 6. Verify trade journal recorded the smoke order
    trades_dir = tmp_path / "trades"
    assert (trades_dir / "trades.jsonl").exists()
    assert (trades_dir / "trades.csv").exists()
    trade_lines = (trades_dir / "trades.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(trade_lines) >= 1
    trade_obj = json.loads(trade_lines[0])
    assert trade_obj["symbol"] == "EURUSD"
    assert trade_obj["mode"] == "MOCK"

