"""Secret-leak and static security tests.

This is the automated guard for the non-negotiable rules:
  * no credential literal anywhere in the FundingPips code paths
  * credentials never appear in repr(), exceptions, logs, or telemetry
  * MetaTrader5 is never a module-level import (research must work without it)
  * execution modules never import a concrete alpha implementation
  * read-only CLIs are structurally incapable of placing an order

The scan is AST-based rather than grep-based, so a string that merely MENTIONS
"password" in a docstring or a keyword argument name is not a false positive, while a
real credential literal is caught.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import List, Tuple

import pytest

from src.execution.errors import ExecutionError, MT5AuthenticationError
from src.execution.redaction import (
    MASK,
    Secret,
    clear_registered_secrets,
    mask_account,
    redact,
    redact_mapping,
    register_secret,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Every file that is part of the FundingPips execution surface.
FUNDINGPIPS_FILES = [
    "config/fundingpips.py",
    "src/execution/base.py",
    "src/execution/errors.py",
    "src/execution/execution_profile.py",
    "src/execution/fundingpips_mt5.py",
    "src/execution/models.py",
    "src/execution/redaction.py",
    "src/execution/symbol_mapper.py",
    "src/execution/telemetry.py",
    "research/execution/calibrate_fundingpips.py",
    "scripts/fundingpips_cli.py",
    "scripts/fundingpips_connection_check.py",
    "scripts/fundingpips_symbol_probe.py",
    "scripts/fundingpips_record_spread.py",
    "scripts/fundingpips_smoke_order.py",
]

#: Patterns that must never appear as string literals in those files.
FORBIDDEN_LITERAL_PATTERNS = (
    re.compile(r"\bFUNDINGPIPS_MT5_PASSWORD\s*=\s*[\"'][^\"']+[\"']"),
    re.compile(r"\bMT5_PASSWORD\s*=\s*[\"'][^\"']+[\"']"),
    re.compile(r"(?i)\b(password|passwd|investor_password)\s*=\s*[\"'][A-Za-z0-9!@#$%^&*_\-]{6,}[\"']"),
)

#: Known secret shapes: long digit runs that could be a real account number.
ACCOUNT_LITERAL_RE = re.compile(r"\b[0-9]{7,}\b")

#: Account numbers that are legitimately not secrets (test fixtures/docs use these).
ALLOWED_NUMERIC_LITERALS = {"10009", "10008", "10004", "10030", "100000", "1000000",
                            "12345678", "1700000000"}


@pytest.fixture(autouse=True)
def _clean_secrets():
    clear_registered_secrets()
    yield
    clear_registered_secrets()


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def _tree(rel: str) -> ast.Module:
    return ast.parse(_read(rel), filename=rel)


# ============================================================ case 39: AST scan
@pytest.mark.parametrize("rel", FUNDINGPIPS_FILES)
def test_no_forbidden_credential_literals(rel):
    """AST-level scan: no string literal in the execution surface is a credential."""
    tree = _tree(rel)
    offenders: List[Tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            for pattern in FORBIDDEN_LITERAL_PATTERNS:
                match = pattern.search(text)
                if match:
                    offenders.append((node.lineno, match.group(0)))
    assert not offenders, "credential-like literal(s) in %s: %s" % (rel, offenders)


@pytest.mark.parametrize("rel", FUNDINGPIPS_FILES)
def test_no_hardcoded_account_number_literals(rel):
    """A real MT5 login is a 7+ digit run; none may be baked into the source."""
    tree = _tree(rel)
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            if node.value > 999_999 and node.value not in {1_700_000_000}:
                offenders.append((node.lineno, node.value))
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for match in ACCOUNT_LITERAL_RE.finditer(node.value):
                if match.group(0) not in ALLOWED_NUMERIC_LITERALS:
                    offenders.append((node.lineno, match.group(0)))
    assert not offenders, "hardcoded long numeric literal(s) in %s: %s" % (rel, offenders)


def test_env_example_has_no_real_values():
    """The tracked template must contain only placeholders."""
    text = _read(".env.example")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key in ("APP_MODE", "LOG_LEVEL", "FUNDINGPIPS_MT5_SERVER", "FUNDINGPIPS_ALLOW_TRADING",
                   "EXECUTION_PROVIDER"):
            continue
        assert not value or value in ("", ), (
            "the tracked .env.example must not carry a real value for %s" % key
        )


def test_env_example_documents_the_required_variables():
    text = _read(".env.example")
    for var in ("FUNDINGPIPS_MT5_LOGIN", "FUNDINGPIPS_MT5_PASSWORD",
                "FUNDINGPIPS_MT5_SERVER", "FUNDINGPIPS_MT5_PATH",
                "FUNDINGPIPS_ALLOW_TRADING"):
        assert var in text, "%s must be documented in .env.example" % var
    assert "FundingPips-Trial" in text


def test_gitignore_excludes_env_but_keeps_example():
    text = _read(".gitignore")
    lines = [l.strip() for l in text.splitlines()]
    assert ".env" in lines
    assert ".env.*" in lines
    assert "!.env.example" in lines


def test_gitignore_excludes_execution_telemetry():
    text = _read(".gitignore")
    assert "data/execution/" in text


# ================================================== module-level import hygiene
@pytest.mark.parametrize("rel", FUNDINGPIPS_FILES)
def test_mt5_is_never_a_module_level_import(rel):
    """MetaTrader5 must only ever be imported lazily inside a function."""
    tree = _tree(rel)
    for node in tree.body:  # module level only
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("MetaTrader5")
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("MetaTrader5")


def test_research_imports_without_mt5_installed():
    """The research stack must import on a machine with no MT5 runtime."""
    import importlib

    for module in ("research.data_admission", "research.mtf", "research.phase3_vr_veto",
                   "research.execution.calibrate_fundingpips", "src.execution"):
        importlib.import_module(module)


@pytest.mark.parametrize("rel", FUNDINGPIPS_FILES)
def test_execution_never_imports_a_concrete_alpha(rel):
    """Execution must not depend on Alpha v2 or any concrete strategy."""
    tree = _tree(rel)
    forbidden = ("src.strategy.alpha_v2_mtf", "src.strategy.zscore_scalper",
                 "src.strategy.trend_v1", "src.strategy.sample_strategy")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name not in forbidden, "%s imports %s" % (rel, alias.name)
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert module not in forbidden, "%s imports %s" % (rel, module)


def test_read_only_clis_cannot_enable_orders():
    """connection_check, symbol_probe and record_spread must never opt into trading."""
    for rel in ("scripts/fundingpips_connection_check.py",
                "scripts/fundingpips_symbol_probe.py",
                "scripts/fundingpips_record_spread.py"):
        tree = _tree(rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg == "allow_order":
                        value = keyword.value
                        assert isinstance(value, ast.Constant) and value.value is False, (
                            "%s must pass allow_order=False" % rel
                        )
        # And none of them may call a write method.
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in ("place_order", "close_position", "modify_position"), (
                    "%s must not call %s" % (rel, node.attr)
                )


def test_smoke_order_requires_both_gates_in_source():
    """The smoke utility must read BOTH the env flag and the explicit CLI flag."""
    text = _read("scripts/fundingpips_smoke_order.py")
    assert "--allow-order" in text
    assert "cfg.allow_trading and args.allow_order" in text


def test_config_documents_that_env_flag_alone_is_insufficient():
    """The safety contract must be stated where a reader will find it."""
    text = _read("config/fundingpips.py")
    assert "BOTH" in text
    assert "explicit --allow-order" in text or "explicit order flag" in text


# ====================================================== repr / exception leaks
def test_secret_repr_and_str_are_masked():
    secret = Secret("top-secret-value")
    assert "top-secret-value" not in repr(secret)
    assert "top-secret-value" not in str(secret)
    assert MASK in repr(secret)


def test_secret_reveal_is_explicit():
    secret = Secret("top-secret-value")
    assert secret.reveal() == "top-secret-value"


def test_secret_cannot_be_pickled():
    import pickle

    with pytest.raises(TypeError):
        pickle.dumps(Secret("x"))


def test_exception_message_is_redacted():
    register_secret("leaky-password")
    exc = ExecutionError("login failed with leaky-password")
    assert "leaky-password" not in str(exc)
    assert "leaky-password" not in repr(exc)


def test_exception_context_is_redacted():
    register_secret("leaky-password")
    exc = ExecutionError("failed", detail="value was leaky-password")
    assert "leaky-password" not in str(exc.context)


def test_exception_context_preserves_structure():
    """Structured context must stay structured so diagnostics remain readable."""
    exc = ExecutionError("ambiguous", candidates=["EURUSD.a", "EURUSD.b"])
    assert exc.context["candidates"] == ["EURUSD.a", "EURUSD.b"]


def test_authentication_exception_never_carries_the_password():
    exc = MT5AuthenticationError("login failed for account ****5678")
    assert "****5678" in str(exc)
    assert not hasattr(exc, "password")


def test_redact_strips_key_value_pairs():
    assert "hunter2" not in redact("password=hunter2")
    assert "hunter2" not in redact('{"password": "hunter2"}')
    assert "hunter2" not in redact("investor_password='hunter2'")


def test_redact_strips_long_digit_runs():
    assert "12345678" not in redact("account 12345678 connected")


def test_redact_leaves_legitimate_numbers_alone():
    """Redaction must not destroy prices, spreads, or timestamps."""
    text = "bid=1.08501 ask=1.08512 spread=1.2 pips equity=10000.00"
    assert redact(text) == text


def test_redact_masks_registered_secrets_longest_first():
    register_secret("abc")
    register_secret("abcdef")
    assert "abcdef" not in redact("value abcdef here")


def test_mask_account_reveals_only_trailing_digits():
    assert mask_account(12345678) == "****5678"
    assert mask_account("12345678") == "****5678"
    assert mask_account(None) == MASK
    assert mask_account("") == MASK


def test_mask_account_short_input_is_fully_masked():
    assert mask_account("123") == "***"


def test_redact_mapping_blocks_secret_keys():
    payload = redact_mapping({"password": "hunter2", "server": "FundingPips-Trial", "equity": 100.0})
    assert payload["password"] == MASK
    assert payload["server"] == "FundingPips-Trial"
    assert payload["equity"] == 100.0


def test_redact_mapping_handles_secret_objects():
    payload = redact_mapping({"pw": Secret("hunter2")})
    assert payload["pw"] == MASK


def test_redact_is_safe_on_none_and_non_strings():
    assert redact(None) == ""
    assert redact(12345) == "12345"
    assert redact(1.5) == "1.5"


def test_no_credential_in_any_fundingpips_docstring_or_comment():
    """A grep-level second opinion over the whole execution surface."""
    for rel in FUNDINGPIPS_FILES:
        text = _read(rel)
        # Any string that looks like an assignment of a real secret.
        assert not re.search(r"FUNDINGPIPS_MT5_PASSWORD\s*=\s*[\"'][^\"']+[\"']", text), rel
        assert "investor password" not in text.lower() or "never" in text.lower(), rel

