"""Repository-anchored paths: one place that decides where data lives.

The bug this module eliminates
------------------------------
Data paths were declared as RELATIVE literals in six different modules:

    src/execution/telemetry.py        RAW_DIR, CANONICAL_DIR
    src/execution/execution_profile.py PROFILE_DIR
    scripts/fundingpips_readiness.py  Path("data/real")
    scripts/fundingpips_research.py   DATA_DIR
    research/data/real_feed.py        OUT_DIR

A relative path is resolved against the process CWD at the moment it is USED,
not when it is declared. So the same constant names a different file depending on
where the process was started. Two concrete consequences observed in this repo:

  1. A writer running from the repo root and a reader running from elsewhere
     silently disagreed about which file they meant.
  2. A test that changed directory wrote a fabricated
     connection_evidence.json into the REAL data directory, and the live
     readiness check then reported three criteria PASSED with no MT5 login.

Both are the same defect. The fix is to resolve every data path against the
repository root exactly once, here, and have every other module import from it.

Nothing in this module performs I/O. It only computes absolute locations.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

_MARKERS = ("requirements.txt", ".gitignore", "pyproject.toml")


def find_repo_root(start: Optional[Path] = None) -> Path:
    """Walk upward from this file until a repository marker is found.

    Falls back to the parent of the package directory rather than raising, so an
    unusual checkout still yields a usable (if approximate) root instead of
    crashing every import in the project.
    """
    here = Path(start or __file__).resolve()
    for candidate in [here] + list(here.parents):
        if candidate.is_dir() and any((candidate / m).exists() for m in _MARKERS):
            return candidate
    return here.parents[2]


#: Absolute repository root. Computed once, at import, and never recomputed from
#: the CWD.
REPO_ROOT: Path = find_repo_root()

#: Root for all repository-managed data.
DATA_DIR: Path = REPO_ROOT / "data"

#: Real market data (research input).
REAL_DATA_DIR: Path = DATA_DIR / "real"

#: FundingPips execution evidence (calibration input for readiness).
EXECUTION_DIR: Path = DATA_DIR / "execution" / "fundingpips"
EXECUTION_RAW_DIR: Path = EXECUTION_DIR / "raw"
EXECUTION_CANONICAL_DIR: Path = EXECUTION_DIR / "canonical"
EXECUTION_PROFILE_PATH: Path = EXECUTION_DIR / "execution_profile.json"
CONNECTION_EVIDENCE_PATH: Path = EXECUTION_DIR / "connection_evidence.json"
READINESS_REPORT_PATH: Path = EXECUTION_DIR / "stage1_readiness.json"

#: Dedicated directory for all executed trades (testing, smoke order, production).
TRADES_DIR: Path = DATA_DIR / "trades"
TRADES_LEDGER_PATH: Path = TRADES_DIR / "trades.jsonl"
TRADES_CSV_PATH: Path = TRADES_DIR / "trades.csv"


def assert_absolute() -> None:
    """Every declared path must be absolute. Used by a test, not at import.

    Kept out of import time so a partially-populated checkout can still import
    the module to report what is wrong.
    """
    for name, value in sorted(globals().items()):
        if name.isupper() and isinstance(value, Path):
            if not value.is_absolute():
                raise AssertionError(
                    "%s is relative (%s); a CWD-dependent path is the defect "
                    "this module exists to remove" % (name, value))
