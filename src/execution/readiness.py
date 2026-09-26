"""Stage-1 readiness gate: may a smoke order be placed yet?

The problem this solves
----------------------
A smoke order is the first irreversible action in this project: it moves real
money on a real (trial) account. Before it runs, three pieces of EVIDENCE should
exist, and none of them should be a default value baked into a CLI:

  1. These credentials actually log in, against the EXPECTED server.
  2. The symbol really resolves, and its contract metadata is known.
  3. The spread has been MEASURED on this account, not assumed.

Without (3) the smoke order's --max-spread-pips ceiling is a guess, and the
cost attribution it prints afterwards is uninterpretable.

What this module is NOT
-----------------------
It is not a strategy check and it does not look at P&L. Passing this gate means
"we know enough about the venue to safely send one probe order", not "the
strategy is good". Those are different questions and conflating them is how
people end up live-trading a hypothesis.

Design
------
The gate is a pure function over an evidence bundle, so it can be unit-tested
without MT5, a network, or credentials. The CLI wrapper does the I/O.
"""
from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

#: Where stage-1 evidence is written.
#:
#: Anchored to the REPOSITORY ROOT rather than left relative. A relative default
#: resolves against the process CWD at call time, so any code that changes
#: directory (a test using monkeypatch.chdir, a service started elsewhere) would
#: silently read or write the wrong location - or the REAL one. That is how a
#: fabricated connection_evidence.json ended up in the live data directory and
#: made three readiness criteria report PASSED without any MT5 login.
from src.paths import CONNECTION_EVIDENCE_PATH as CONNECTION_PATH
from src.paths import EXECUTION_DIR as READINESS_DIR
from src.paths import READINESS_REPORT_PATH as READINESS_PATH

#: Written by fundingpips_connection_check after a successful login. Its absence
#: is what makes the three connection criteria fail, which is correct: an
#: unverified login is not a verified login.
CONNECTION_PATH = READINESS_DIR / "connection_evidence.json"

#: A spread measurement older than this is stale and must be re-taken.
MAX_EVIDENCE_AGE_DAYS = 30

#: Fewer samples than this is not a measurement, it is an anecdote.
MIN_SPREAD_SAMPLES = 200

#: If the observed p95 spread exceeds this, the venue is too unstable for a
#: first smoke order and the operator should re-measure at a calmer time.
MAX_ACCEPTABLE_P95_SPREAD_PIPS = 5.0


@dataclass
class Check:
    """One readiness criterion, with the reason it passed or failed."""

    key: str
    passed: bool
    detail: str
    blocking: bool = True

    def to_dict(self) -> Dict[str, object]:
        return {"key": self.key, "passed": self.passed,
                "detail": self.detail, "blocking": self.blocking}


@dataclass
class ReadinessReport:
    checks: List[Check] = field(default_factory=list)
    generated_utc: str = ""

    @property
    def blocking_failures(self) -> List[Check]:
        return [c for c in self.checks if c.blocking and not c.passed]

    @property
    def ready(self) -> bool:
        return not self.blocking_failures

    @property
    def warnings(self) -> List[Check]:
        return [c for c in self.checks if not c.blocking and not c.passed]

    def summary(self) -> Dict[str, object]:
        return {
            "ready_for_smoke_order": self.ready,
            "generated_utc": self.generated_utc,
            "passed": sum(1 for c in self.checks if c.passed),
            "total": len(self.checks),
            "blocking_failures": [c.to_dict() for c in self.blocking_failures],
            "warnings": [c.to_dict() for c in self.warnings],
            "checks": [c.to_dict() for c in self.checks],
        }

    def render(self) -> str:
        lines = []
        for c in self.checks:
            mark = "PASS" if c.passed else ("FAIL" if c.blocking else "WARN")
            lines.append("  [%-4s] %-28s %s" % (mark, c.key, c.detail))
        return "\n".join(lines)


# ------------------------------------------------------------------ helpers

def _age_days(iso: Optional[str]) -> Optional[float]:
    if not iso:
        return None
    try:
        ts = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_dt.timezone.utc)
    delta = _dt.datetime.now(_dt.timezone.utc) - ts
    return delta.total_seconds() / 86400.0


def evaluate_readiness(evidence: Dict[str, Any],
                       max_age_days: float = MAX_EVIDENCE_AGE_DAYS,
                       min_samples: int = MIN_SPREAD_SAMPLES,
                       max_p95: float = MAX_ACCEPTABLE_P95_SPREAD_PIPS,
                       now_iso: Optional[str] = None) -> ReadinessReport:
    """Decide whether the evidence bundle justifies a smoke order.

    Kept pure: everything it needs is in the argument. That is what makes the
    gate testable and stops it from silently depending on ambient state.
    """
    checks: List[Check] = []

    # --- 1. connection evidence -------------------------------------------
    conn = evidence.get("connection") or {}
    logged_in = bool(conn.get("logged_in"))
    checks.append(Check(
        "connection_verified", logged_in,
        "credentials accepted by the terminal" if logged_in
        else "no successful login recorded - run fundingpips_connection_check"))

    server_ok = bool(conn.get("server_matches_expected"))
    checks.append(Check(
        "server_is_expected", server_ok,
        "server matches FUNDINGPIPS_MT5_SERVER" if server_ok
        else "connected server differs from the configured expected server"))

    acct_ok = bool(conn.get("account_number_present"))
    checks.append(Check(
        "account_identified", acct_ok,
        "account number present and valid" if acct_ok
        else "no valid account number recorded"))

    # --- 2. symbol evidence -----------------------------------------------
    sym = evidence.get("symbol") or {}
    resolved = bool(sym.get("resolved"))
    checks.append(Check(
        "symbol_resolved", resolved,
        "resolved to %s" % sym.get("provider_symbol") if resolved
        else "EURUSD did not resolve - run fundingpips_symbol_probe"))

    pip_ok = bool(sym.get("pip_size_positive"))
    checks.append(Check(
        "pip_size_known", pip_ok,
        "pip size %s" % sym.get("pip_size") if pip_ok
        else "pip size missing or non-positive"))

    tradable = bool(sym.get("is_tradable"))
    checks.append(Check(
        "symbol_tradable", tradable,
        "trade mode allows orders" if tradable
        else "symbol is not in a tradable mode (disabled/close-only)"))

    # --- 3. spread evidence (the one that actually matters) ---------------
    spread = evidence.get("spread") or {}
    samples = int(spread.get("samples") or 0)
    enough = samples >= min_samples
    checks.append(Check(
        "spread_measured", enough,
        "%d samples (need >= %d)" % (samples, min_samples) if not enough
        else "%d samples" % samples))

    age = _age_days(spread.get("captured_utc"))
    fresh = age is not None and age <= max_age_days
    checks.append(Check(
        "spread_fresh", fresh,
        "measured %.1f days ago (limit %.0f)" % (age, max_age_days) if age is not None
        else "no capture timestamp on the spread evidence"))

    p95 = spread.get("p95")
    p95_ok = p95 is not None and float(p95) <= max_p95
    checks.append(Check(
        "spread_stable", p95_ok,
        "p95 spread %.2f pip (ceiling %.2f)" % (p95, max_p95) if p95 is not None
        else "no p95 spread recorded"))

    p50 = spread.get("p50")
    p50_ok = p50 is not None and float(p50) > 0
    checks.append(Check(
        "spread_sane", p50_ok,
        "median %.2f pip" % p50 if p50_ok
        else "median spread missing or zero - crossed quotes?"))
    # A zero/absent median usually means the recorder never saw a valid quote.

    # --- 4. safety configuration ------------------------------------------
    cfg = evidence.get("config") or {}
    flag_off = not bool(cfg.get("allow_trading"))
    checks.append(Check(
        "trading_flag_off", flag_off,
        "FUNDINGPIPS_ALLOW_TRADING is false (safe default)" if flag_off
        else "FUNDINGPIPS_ALLOW_TRADING is TRUE - live orders are unblocked",
        blocking=False))

    # --- 5. strategy-quality context (ADVISORY, deliberately never blocking) --
    #
    # These checks exist to stop an operator from confusing two different
    # questions. The smoke order's own docstring states its purpose: "validate
    # execution plumbing and accounting, NOT profit". Blocking an execution
    # probe on strategy quality would therefore be a category error -- it would
    # refuse to test a plug because the strategy attached to it is unproven.
    #
    # They are surfaced loudly anyway, because a reader who sees "READY" MUST
    # NOT conclude the strategy is any good.
    res = evidence.get("research") or {}
    folds = int(res.get("folds") or 0)
    checks.append(Check(
        "research_ran", folds > 0,
        "%d walk-forward folds recorded" % folds if folds > 0
        else "no walk-forward has been run - you do not yet know if the "
             "strategy survives the rules",
        blocking=False))

    # Concentration: if the best 10 percent of trades carries the whole result,
    # the edge is outliers, not signal. Advisory here, blocking in the research
    # pipeline (grid/cross), which is where it belongs.
    conc = res.get("concentration_ok")
    if conc is not None:
        checks.append(Check(
            "profit_not_outlier_driven", bool(conc),
            "profit survives removing the top decile" if conc
            else "profit vanishes when the best 10 percent of trades is "
                 "removed - the result is outlier-driven",
            blocking=False))

    fals = res.get("falsification_verdict")
    if fals:
        ok = str(fals).upper() == "SURVIVED"
        checks.append(Check(
            "strategy_survived_falsification", ok,
            "falsification verdict: %s" % fals if ok
            else "falsification verdict is %s - the strategy was REJECTED, "
                 "so a smoke order here tests plumbing only, never an idea"
                 % fals,
            blocking=False))

    breadth = res.get("pairs_positive")
    total_pairs = res.get("pairs_total")
    if breadth is not None and total_pairs:
        need = int(res.get("pairs_required") or 4)
        ok = int(breadth) >= need
        checks.append(Check(
            "cross_pair_breadth", ok,
            "%d of %d pairs positive (need %d)" % (breadth, total_pairs, need)
            if ok else
            "only %d of %d pairs positive, below the protocol requirement of %d"
            % (breadth, total_pairs, need),
            blocking=False))

    rep = ReadinessReport(checks=checks,
                          generated_utc=now_iso or _dt.datetime.now(
                              _dt.timezone.utc).isoformat())
    return rep


# ------------------------------------------------------------- persistence

def save_readiness(report: ReadinessReport, path: Optional[Path] = None) -> Path:
    target = Path(path) if path else READINESS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(report.summary(), indent=2), encoding="utf-8")
    tmp.replace(target)          # atomic: a crash cannot leave a half file
    return target


def load_readiness(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    target = Path(path) if path else READINESS_PATH
    if not target.exists():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_connection_evidence(login: int, server: str, expected_server: str,
                             path: Optional[Path] = None) -> Path:
    """Persist the FACT of a successful login, with the account masked.

    Without this the three connection criteria could never pass: the gate read a
    key that nothing ever wrote, so it would report NOT READY forever even on a
    host with valid credentials. That is a dead gate -- it looks like a check
    while being a permanent refusal.

    Only the MASKED login and the server names are stored. The password is never
    written, and neither is the full account number.
    """
    from src.execution.redaction import mask_account

    target = Path(path) if path else CONNECTION_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "logged_in": True,
        "server": str(server),
        "expected_server": str(expected_server),
        "server_matches_expected": str(server) == str(expected_server),
        "account_number_present": bool(login) and int(login) > 0,
        "masked_login": mask_account(int(login)),
        "recorded_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(target)
    return target


def load_connection_evidence(path: Optional[Path] = None) -> Dict[str, Any]:
    """Read persisted login evidence, or an empty dict if none exists."""
    target = Path(path) if path else CONNECTION_PATH
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def build_evidence_from_profile(profile_path: Optional[Path] = None,
                                connection: Optional[Dict[str, Any]] = None,
                                config: Optional[Dict[str, Any]] = None,
                                research: Optional[Dict[str, Any]] = None,
                                connection_path: Optional[Path] = None,
                                spread_dir: Optional[Path] = None,
                                ) -> Dict[str, Any]:
    """Assemble an evidence bundle from the artefacts stage 1 produces.

    Reads three things, each written by a different tool:
      * the login evidence written by fundingpips_connection_check
      * the execution profile written by fundingpips_symbol_probe --save-profile
      * the spread telemetry written by fundingpips_record_spread

    Any of them missing simply produces missing evidence, which the gate then
    rejects. It never invents a value to make itself pass.
    """
    from src.execution.execution_profile import FundingPipsExecutionProfile

    # Explicit argument wins over the persisted file, so a caller can inject
    # facts it just observed without touching disk.
    conn = dict(connection) if connection else load_connection_evidence(
        connection_path)

    bundle: Dict[str, Any] = {
        "connection": conn,
        "symbol": {},
        "spread": {},
        "config": dict(config or {}),
        "research": dict(research or {}),
    }

    # ---- spread telemetry (canonical JSONL) ------------------------------
    # Overridable so a caller (or a test) can point at an isolated directory.
    # Without this the reader was pinned to the real path, which is exactly how
    # a test leaked evidence into the live data directory.
    canonical = Path(spread_dir) if spread_dir else READINESS_DIR / "canonical"
    if canonical.exists():
        samples: List[float] = []
        latest_ts: Optional[str] = None
        for fp in sorted(canonical.glob("*.jsonl")):
            try:
                for line in fp.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    rec = json.loads(line)
                    val = rec.get("spread_pips")
                    if val is None:
                        continue
                    samples.append(float(val))
                    ts = rec.get("local_receipt_utc") or rec.get("timestamp_utc")
                    if ts and (latest_ts is None or ts > latest_ts):
                        latest_ts = ts
            except Exception:
                continue
        if samples:
            samples.sort()

            def pct(q: float) -> float:
                if len(samples) == 1:
                    return samples[0]
                pos = q * (len(samples) - 1)
                lo = int(pos)
                hi = min(lo + 1, len(samples) - 1)
                frac = pos - lo
                return samples[lo] * (1 - frac) + samples[hi] * frac

            bundle["spread"] = {
                "samples": len(samples),
                "p50": round(pct(0.50), 4),
                "p95": round(pct(0.95), 4),
                "captured_utc": latest_ts,
            }

    # ---- symbol metadata --------------------------------------------------
    try:
        data = FundingPipsExecutionProfile.load(profile_path)
        sym = data.get("symbol") or {}
        pip = sym.get("pip_size")
        bundle["symbol"] = {
            "resolved": bool(sym.get("provider_symbol")),
            "provider_symbol": sym.get("provider_symbol"),
            "pip_size": pip,
            "pip_size_positive": bool(pip and float(pip) > 0),
            "is_tradable": bool(sym.get("is_tradable")),
        }
    except Exception:
        pass

    return bundle
