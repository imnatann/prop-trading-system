"""Guard: tests must never write evidence into the REAL data directory.

Why this exists
---------------
A test run using the default evidence path (instead of pytest's tmp_path) wrote
a fabricated connection_evidence.json into data/execution/fundingpips/. The live
readiness check then read it and reported three criteria as PASSED, even though
no MetaTrader login had ever happened on this machine.

That is the exact failure this whole project guards against: evidence that looks
measured but is not. This module makes the mistake detectable at test time rather
than at the moment someone trusts a fabricated green light.

It is a process guard, not a behavioural test: it inspects the source of the
test suite for default-path writes, and asserts that the real directory does not
silently accumulate artefacts during a run.
"""
from __future__ import annotations

import inspect
import pathlib
import re

import pytest

from src.execution import readiness as readiness_mod
from src.execution.readiness import (
    CONNECTION_PATH,
    READINESS_PATH,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_evidence_paths_live_under_the_ignored_directory():
    """Both artefacts must sit in data/execution/, which .gitignore covers."""
    for path in (CONNECTION_PATH, READINESS_PATH):
        assert "data/execution" in str(path).replace("\\", "/"), path


def test_gitignore_covers_the_evidence_directory():
    gi = REPO_ROOT / ".gitignore"
    assert gi.exists(), "no .gitignore at repo root"
    text = gi.read_text()
    assert "data/execution" in text, (
        "evidence artefacts are not gitignored; a fabricated or real file could "
        "be committed")


def test_save_connection_evidence_requires_an_explicit_path_in_tests(tmp_path, monkeypatch):
    """The helper itself must honour the path argument, never ignore it."""
    default_dummy = tmp_path / "default_guard.json"
    monkeypatch.setattr(readiness_mod, "CONNECTION_PATH", default_dummy)
    target = tmp_path / "explicit.json"
    readiness_mod.save_connection_evidence(
        login=12345678, server="FundingPips-Trial",
        expected_server="FundingPips-Trial", path=target)
    assert target.exists()
    assert not default_dummy.exists(), (
        "writing with an explicit path must NOT also touch the default path")


def test_no_test_writes_evidence_without_a_tmp_path():
    """Scan the test suite for writes that would hit the REAL evidence paths.

    A call to save_connection_evidence / save_readiness with no path argument
    lands in data/execution and fabricates evidence for the live CLI.

    Detection is deliberately AST-based rather than textual: a naive regex on
    "path=" produced false positives on calls passing a tmp_path POSITIONALLY,
    and a guard that cries wolf gets deleted. This checks whether a path-like
    argument is actually supplied, however it is spelled.
    """
    import ast

    tests_dir = REPO_ROOT / "tests"
    offenders = []
    targets = {"save_connection_evidence", "save_readiness"}

    for py in tests_dir.rglob("*.py"):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name not in targets:
                continue
            has_path_kw = any(kw.arg == "path" for kw in node.keywords)
            # a positional second argument is also a path (first is the object)
            has_path_pos = len(node.args) >= 2
            if not (has_path_kw or has_path_pos):
                offenders.append("%s:%d %s(...)" % (
                    py.name, node.lineno, name))

    assert not offenders, (
        "these test calls could write REAL evidence into data/execution:\n  "
        + "\n  ".join(offenders))


def test_live_readiness_is_not_ready_on_a_clean_checkout(tmp_path):
    """On a clean checkout with no evidence files, the gate must report NOT READY."""
    from src.execution.readiness import (
        build_evidence_from_profile, evaluate_readiness,
    )
    ev = build_evidence_from_profile(
        profile_path=tmp_path / "absent_profile.json",
        connection_path=tmp_path / "absent_conn.json",
        spread_dir=tmp_path / "absent_spread",
    )
    report = evaluate_readiness(ev)
    assert report.ready is False
    assert "connection_verified" in [c.key for c in report.blocking_failures]
