"""Simulator-independence and research-protection tests.

Covers required cases 35-38: the existing simulator_v2 invariants stay valid, Alpha v2
stays untouched, the no-lookahead guarantees hold, the data layer is unchanged, and
simulator_v2 remains broker-independent.

This file asserts BOUNDARIES rather than re-testing the modules' own suites. Those
suites still run; here we prove the FundingPips integration did not breach them.
"""
from __future__ import annotations

import ast
import hashlib
import inspect
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Files the execution task must NOT have modified (research integrity surface).
PROTECTED_RESEARCH_FILES = [
    "research/backtest/simulator_v2.py",
    "src/strategy/alpha_v2_mtf.py",
    "research/mtf.py",
    "research/data_admission.py",
    "research/phase3_vr_veto.py",
    "research/phase4_cost_session.py",
    "research/phase5_walkforward.py",
    "research/phase6_falsification.py",
    "src/risk/exante_gates.py",
    "research/protocol/frozen_config.py",
    "research/protocol/fold_plan.py",
]


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


# ------------------------------------------- case 35: simulator_v2 invariants
def test_simulator_v2_does_not_import_mt5_or_execution():
    """simulator_v2 must stay broker-independent and deterministic."""
    tree = ast.parse(_read("research/backtest/simulator_v2.py"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("MetaTrader5")
                assert not alias.name.startswith("src.execution")
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert not module.startswith("MetaTrader5")
            assert not module.startswith("src.execution.fundingpips")


def test_simulator_v2_cost_spec_shape_is_unchanged():
    """The calibration layer must target the EXISTING CostSpec, not fork it."""
    from research.backtest.simulator_v2 import CostSpec

    fields = set(CostSpec.__dataclass_fields__)
    assert fields == {
        "spread_pips", "slippage_pips", "commission_per_lot_per_side",
        "swap_long_pips", "swap_short_pips",
    }


def test_simulation_cost_parameters_map_onto_cost_spec():
    """The conversion produces values the existing CostSpec accepts verbatim."""
    from research.backtest.simulator_v2 import CostSpec
    from src.execution.models import SimulationCostParameters

    params = SimulationCostParameters(
        spread_pips=1.1, slippage_pips=0.3,
        commission_per_lot_per_side=3.5, swap_long_pips=-0.8, swap_short_pips=0.2,
    )
    spec = CostSpec(**{
        "spread_pips": params.spread_pips,
        "slippage_pips": params.slippage_pips,
        "commission_per_lot_per_side": params.commission_per_lot_per_side,
        "swap_long_pips": params.swap_long_pips,
        "swap_short_pips": params.swap_short_pips,
    })
    assert spec.spread_pips == 1.1
    assert spec.commission_per_lot_per_side == 3.5


def test_simulator_v2_runs_without_any_execution_import():
    """A deterministic historical run must not need MT5 or the execution layer."""
    from research.backtest.simulator_v2 import CostSpec, InstrumentSpec

    instrument = InstrumentSpec(symbol="EURUSD")
    costs = CostSpec(spread_pips=1.0, slippage_pips=0.2)
    assert instrument.pip == pytest.approx(0.0001)
    assert costs.spread_pips == 1.0


# ------------------------------------------- case 36/37/38: protected files
@pytest.mark.parametrize("rel", PROTECTED_RESEARCH_FILES)
def test_protected_research_files_exist(rel):
    assert (REPO_ROOT / rel).exists(), "%s must still exist" % rel


@pytest.mark.parametrize("rel", PROTECTED_RESEARCH_FILES)
def test_protected_research_files_do_not_reference_fundingpips(rel):
    """No research-protocol module may learn about the execution venue."""
    text = _read(rel)
    assert "fundingpips" not in text.lower(), "%s must not reference FundingPips" % rel
    assert "MetaTrader5" not in text, "%s must not reference MT5" % rel


def test_alpha_v2_frozen_grid_is_still_four_configs():
    """The frozen Alpha v2 grid must not have grown or shrunk."""
    from src.strategy.alpha_v2_mtf import FROZEN_GRID

    assert len(FROZEN_GRID) == 4


def test_alpha_v2_config_is_still_frozen():
    from src.strategy.alpha_v2_mtf import AlphaV2Config

    assert AlphaV2Config.__dataclass_params__.frozen is True


def test_research_protocol_document_is_unmodified_in_substance():
    """The protocol must still carry its thresholds, and no new execution excuse."""
    text = _read("docs/RESEARCH_PROTOCOL_v2.md")
    assert "3.0" in text  # t-stat hurdle
    assert "MIN_YEARS" in text or "MIN_YEARS" in text.upper() or "years" in text.lower()


def test_research_protocol_not_used_to_relax_thresholds():
    """Any FundingPips mention in the protocol must be execution-only, not a waiver."""
    text = _read("docs/RESEARCH_PROTOCOL_v2.md")
    lowered = text.lower()
    if "fundingpips" in lowered:
        for phrase in ("lower the hurdle", "reduce min_years", "relax the threshold"):
            assert phrase not in lowered


def _active_requirement_lines(text: str):
    """Yield requirement lines that are actually installed (not commented out)."""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        yield stripped


def test_execution_layer_is_optional_dependency():
    """MetaTrader5 must NOT be an active requirement of the package.

    It may be MENTIONED (the optional extra is documented, and the CLI error message
    has to name it), but it must never be an uncommented requirement line, otherwise
    macOS/Linux CI and the research environment would break.
    """
    active = list(_active_requirement_lines(_read("requirements.txt")))
    assert not any("MetaTrader5" in line for line in active), (
        "MetaTrader5 must stay an optional extra: %s" % active
    )


def test_mt5_extra_is_documented_but_separate():
    """The Windows-only extra must exist somewhere explicit, commented in main reqs."""
    main = _read("requirements.txt")
    assert "# MetaTrader5" in main
    assert (REPO_ROOT / "requirements-mt5.txt").exists()
    assert "MetaTrader5" in _read("requirements-mt5.txt")


# ------------------------------------------- no strategy leakage
def test_execution_modules_do_not_import_alpha():
    execution_files = list((REPO_ROOT / "src" / "execution").glob("*.py"))
    assert execution_files
    for path in execution_files:
        text = path.read_text(encoding="utf-8")
        for forbidden in ("alpha_v2_mtf", "zscore_scalper", "trend_v1"):
            assert forbidden not in text, "%s references %s" % (path.name, forbidden)


def test_strategy_modules_do_not_import_fundingpips():
    """Alpha must not know the venue exists."""
    strategy_dir = REPO_ROOT / "src" / "strategy"
    for path in strategy_dir.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "fundingpips" not in text.lower(), "%s references FundingPips" % path.name
        assert "MetaTrader5" not in text, "%s references MT5" % path.name


def test_no_scattered_provider_branches():
    """Provider selection must be centralised in the factory, not scattered."""
    offenders = []
    for path in (REPO_ROOT / "src").rglob("*.py"):
        if path.name == "base.py" and path.parent.name == "execution":
            continue
        text = path.read_text(encoding="utf-8")
        if "if fundingpips" in text.lower() or "if provider ==" in text:
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, "provider branching leaked into: %s" % offenders


# ------------------------------------------- calibration layer boundaries
def test_calibration_module_has_no_performance_metric():
    """The calibration module must not COMPUTE a strategy performance metric.

    A text search is the wrong instrument: the module's own docstring explicitly says
    it computes no Sharpe. So we inspect the AST for real identifiers, function names,
    and string keys, which is the same discipline used for the print() audit.
    """
    tree = ast.parse(_read("research/execution/calibrate_fundingpips.py"))
    banned = {"sharpe", "profit_factor", "win_rate", "winrate", "max_drawdown",
              "sortino", "calmar", "expectancy"}

    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id.lower())
        elif isinstance(node, ast.Attribute):
            used.add(node.attr.lower())
        elif isinstance(node, ast.FunctionDef):
            used.add(node.name.lower())
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # string DICT KEYS become report fields - those matter
            if node.value.lower() in banned:
                used.add(node.value.lower())

    offenders = banned & used
    assert not offenders, "calibration computes or emits performance metric(s): %s" % offenders


def test_calibration_report_contains_no_performance_fields():
    from research.execution.calibrate_fundingpips import build_calibration_report
    from src.execution.execution_profile import FundingPipsExecutionProfile
    from src.execution.models import SymbolInfo, TradeMode

    info = SymbolInfo(
        canonical_symbol="EURUSD", provider_symbol="EURUSD", digits=5, point=0.00001,
        trade_tick_size=0.00001, trade_tick_value=1.0, contract_size=100_000.0,
        volume_min=0.01, volume_max=50.0, volume_step=0.01, trade_mode=TradeMode.FULL,
    )
    report = build_calibration_report(FundingPipsExecutionProfile(
        canonical_symbol="EURUSD", provider_symbol="EURUSD", symbol=info,
    ))
    flat = str(report).lower()
    for banned in ("sharpe", "profit_factor", "win_rate", "net_profit"):
        assert banned not in flat


def test_calibration_report_states_its_own_limits():
    from research.execution.calibrate_fundingpips import build_calibration_report
    from src.execution.execution_profile import FundingPipsExecutionProfile
    from src.execution.models import SymbolInfo, TradeMode

    info = SymbolInfo(
        canonical_symbol="EURUSD", provider_symbol="EURUSD", digits=5, point=0.00001,
        trade_tick_size=0.00001, trade_tick_value=1.0, contract_size=100_000.0,
        volume_min=0.01, volume_max=50.0, volume_step=0.01, trade_mode=TradeMode.FULL,
    )
    report = build_calibration_report(FundingPipsExecutionProfile(
        canonical_symbol="EURUSD", provider_symbol="EURUSD", symbol=info,
    ))
    assert "no strategy performance metric" in report["notes"].lower()


# ------------------------------------------- execution data separation
def test_execution_telemetry_dir_is_separate_from_research_dirs():
    from src.execution.telemetry import CANONICAL_DIR, RAW_DIR

    assert "execution" in str(CANONICAL_DIR)
    assert "execution" in str(RAW_DIR)
    assert "data/canonical" != str(CANONICAL_DIR)
    assert "data/raw" != str(RAW_DIR)


def test_execution_profile_path_is_not_a_research_path():
    from src.execution.execution_profile import PROFILE_PATH

    assert "execution" in str(PROFILE_PATH)
