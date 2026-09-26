"""Guard: every data path must be repository-anchored, never CWD-relative.

Why this exists
---------------
Data paths were declared as relative literals in six modules. A relative path is
resolved against the process CWD when it is USED, so the same constant named
different files depending on where the process started. That defect produced a
fabricated connection_evidence.json in the real data directory, which made three
readiness criteria report PASSED with no MetaTrader login.

The fix was src/paths.py. These tests make sure the defect cannot come back: they
scan the source tree for relative data-path literals and assert that the path
constants are absolute and CWD-independent.
"""
from __future__ import annotations

import ast
import os
import pathlib

import pytest

from src import paths as paths_mod
from src.paths import (
    CONNECTION_EVIDENCE_PATH,
    DATA_DIR,
    EXECUTION_CANONICAL_DIR,
    EXECUTION_DIR,
    EXECUTION_PROFILE_PATH,
    EXECUTION_RAW_DIR,
    READINESS_REPORT_PATH,
    REAL_DATA_DIR,
    REPO_ROOT,
    assert_absolute,
)

REPO = pathlib.Path(__file__).resolve().parents[2]


# ============================================================== the module

def test_every_declared_path_is_absolute():
    assert_absolute()          # raises AssertionError on any relative path


def test_all_path_constants_are_absolute():
    for name in ("REPO_ROOT", "DATA_DIR", "REAL_DATA_DIR", "EXECUTION_DIR",
                 "EXECUTION_RAW_DIR", "EXECUTION_CANONICAL_DIR",
                 "EXECUTION_PROFILE_PATH", "CONNECTION_EVIDENCE_PATH",
                 "READINESS_REPORT_PATH"):
        value = getattr(paths_mod, name)
        assert value.is_absolute(), "%s = %s" % (name, value)


def test_repo_root_actually_is_the_repo_root():
    assert (REPO_ROOT / "requirements.txt").exists()
    assert (REPO_ROOT / "src").is_dir()


def test_evidence_paths_live_under_the_gitignored_directory():
    for p in (EXECUTION_PROFILE_PATH, CONNECTION_EVIDENCE_PATH,
              READINESS_REPORT_PATH):
        rel = str(p.relative_to(REPO_ROOT)).replace("\\", "/")
        assert rel.startswith("data/execution/"), rel


def test_real_data_sits_under_data():
    assert str(REAL_DATA_DIR.relative_to(DATA_DIR)).replace("\\", "/") == "real"


# ======================================================= CWD independence

def test_paths_do_not_change_when_the_cwd_changes(tmp_path, monkeypatch):
    before = (EXECUTION_PROFILE_PATH, CONNECTION_EVIDENCE_PATH, REAL_DATA_DIR)
    monkeypatch.chdir(tmp_path)
    after = (EXECUTION_PROFILE_PATH, CONNECTION_EVIDENCE_PATH, REAL_DATA_DIR)
    assert before == after


def test_relative_literals_would_have_changed(tmp_path, monkeypatch):
    """Demonstrates the defect the module removes, so it is not forgotten."""
    monkeypatch.chdir(tmp_path)
    naive = pathlib.Path("data/execution/fundingpips/connection_evidence.json")
    assert naive.resolve() != CONNECTION_EVIDENCE_PATH.resolve()


# ============================================ no relative literals remain

def test_no_relative_data_path_literals_in_source():
    """Scan src/, scripts/, config/ and research/ for Path("data/...") literals.

    A docstring mention is fine; an assignment is not.
    """
    offenders = []
    for folder in ("src", "scripts", "config", "research"):
        root = REPO / folder
        if not root.is_dir():
            continue
        for py in root.rglob("*.py"):
            if "__pycache__" in str(py):
                continue
            try:
                tree = ast.parse(py.read_text(encoding="utf-8", errors="ignore"))
            except SyntaxError:
                continue
            # docstrings are string literals; only flag real expressions
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                name = getattr(fn, "id", None) or getattr(fn, "attr", None)
                if name not in ("Path", "PosixPath"):
                    continue
                if not node.args:
                    continue
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if arg.value.startswith("data/"):
                        offenders.append("%s:%d %s" % (
                            py.relative_to(REPO), node.lineno, arg.value))
    # src/paths.py is the one legitimate place, and it uses REPO_ROOT / "data".
    assert not offenders, (
        "relative data-path literals found; import from src.paths instead:\n  "
        + "\n  ".join(offenders))


# ==================================== consumers actually use the module

def test_telemetry_uses_the_anchored_paths():
    from src.execution import telemetry
    assert telemetry.RAW_DIR == EXECUTION_RAW_DIR
    assert telemetry.CANONICAL_DIR == EXECUTION_CANONICAL_DIR


def test_execution_profile_uses_the_anchored_paths():
    from src.execution import execution_profile as ep
    assert ep.PROFILE_DIR == EXECUTION_DIR
    assert ep.PROFILE_PATH == EXECUTION_PROFILE_PATH


def test_readiness_uses_the_anchored_paths():
    from src.execution import readiness
    assert readiness.READINESS_DIR == EXECUTION_DIR
    assert readiness.CONNECTION_PATH == CONNECTION_EVIDENCE_PATH
    assert readiness.READINESS_PATH == READINESS_REPORT_PATH


def test_real_feed_uses_the_anchored_paths():
    from research.data import real_feed
    assert real_feed.OUT_DIR == REAL_DATA_DIR


def test_research_cli_uses_the_anchored_paths():
    from scripts import fundingpips_research as cli
    assert cli.DATA_DIR == REAL_DATA_DIR
