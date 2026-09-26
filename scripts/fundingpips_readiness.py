"""Stage-1 readiness check: is the evidence sufficient to fire a smoke order?

    python -m scripts.fundingpips_readiness
    python -m scripts.fundingpips_readiness --json

Read-only and credential-free. Runs anywhere, including macOS/Linux where
MetaTrader5 does not exist, because it only inspects evidence that other tools
have already recorded.

Exit codes
    0  ready   - every blocking criterion is satisfied
    3  not ready - at least one BLOCKING criterion failed
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

from scripts.fundingpips_cli import EXIT_MISSING_CREDENTIALS, EXIT_OK, banner, field, safe_main
from src.execution.readiness import (
    MAX_EVIDENCE_AGE_DAYS,
    MIN_SPREAD_SAMPLES,
    build_evidence_from_profile,
    evaluate_readiness,
    load_readiness,
    save_readiness,
)

EXIT_NOT_READY = 3


def _config_flags() -> dict:
    """Read the safety flag WITHOUT requiring credentials to be valid."""
    try:
        from config.fundingpips import load_dotenv_once
        load_dotenv_once()
    except Exception:
        pass
    import os
    return {
        "allow_trading": (os.environ.get("FUNDINGPIPS_ALLOW_TRADING", "")
                          .strip().lower() in ("1", "true", "yes", "on")),
    }


def _research_evidence() -> dict:
    """Strategy-quality context. ADVISORY: it can never block a smoke order.

    The smoke order validates plumbing, not profit. Refusing to test the pipe
    because the strategy is unproven would be a category error. These values are
    surfaced anyway so that a reader seeing READY cannot mistake it for
    "the strategy works".
    """
    from research.data.real_feed import load_csv
    from research.backtest.walkforward_real import build_folds

    out = {"folds": 0}
    target = None
    from src.paths import REAL_DATA_DIR
    for cand in sorted(REAL_DATA_DIR.glob("*_1d.csv")):
        try:
            candles = load_csv(cand)
            folds = build_folds(candles)
            if folds:
                out["folds"] = len(folds)
                out["source"] = str(cand)
                target = cand
                break
        except Exception:
            continue

    if target is None:
        return out

    # Only compute the expensive checks when the caller asks for them, so the
    # default readiness run stays instant.
    if not os.environ.get("FUNDINGPIPS_READINESS_DEEP"):
        return out

    try:
        from research.backtest.cross_sectional import cross_sectional_walk_forward
        rep = cross_sectional_walk_forward()
        summ = rep.summary()
        out["pairs_positive"] = summ["positive_pairs"]
        out["pairs_total"] = summ["pairs_evaluated"]
        out["pairs_required"] = summ["required_positive_pairs"]
        out["falsification_verdict"] = summ["verdict"]
    except Exception:
        pass
    return out


def run(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check whether stage-1 evidence justifies a smoke order.")
    parser.add_argument("--symbol", default="EURUSD")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    parser.add_argument("--profile-path", default=None)
    parser.add_argument("--evidence-dir", default=None,
                        help="Evidence directory containing profile and connection evidence")
    parser.add_argument("--save", action="store_true",
                        help="Persist the report to data/execution/fundingpips/")
    parser.add_argument("--save-path", default=None,
                        help="Override output path for the saved readiness report")
    args = parser.parse_args(argv)

    ev_dir = Path(args.evidence_dir) if args.evidence_dir else None
    prof_path = Path(args.profile_path) if args.profile_path else (
        (ev_dir / "execution_profile.json") if ev_dir and (ev_dir / "execution_profile.json").exists() else None
    )
    conn_path = (ev_dir / "connection_evidence.json") if ev_dir else None
    spread_dir = (ev_dir / "canonical") if ev_dir else None

    evidence = build_evidence_from_profile(
        profile_path=prof_path,
        connection_path=conn_path,
        spread_dir=spread_dir,
        config=_config_flags(),
        research=_research_evidence(),
    )
    # build_evidence_from_profile now loads persisted login evidence itself.
    # Absent means NOT VERIFIED, which is the correct default: no file, no login.
    evidence.setdefault("connection", {})

    report = evaluate_readiness(evidence)

    if args.json:
        print(json.dumps(report.summary(), indent=2))
    else:
        banner("FundingPips stage-1 readiness: %s" % args.symbol)
        print(report.render())
        print()
        s = report.summary()
        field("Criteria passed", "%d / %d" % (s["passed"], s["total"]))
        field("Blocking failures", len(report.blocking_failures))
        field("Warnings", len(report.warnings))
        field("Requirements",
              ">=%d spread samples, <=%d days old, p95<=%.1fpip"
              % (MIN_SPREAD_SAMPLES, MAX_EVIDENCE_AGE_DAYS, 5.0))
        print()
        if report.ready:
            print("=" * 68)
            print("READY: evidence is sufficient to send ONE smoke order.")
            print("=" * 68)
            print()
            print("WHAT THIS MEANS   : the VENUE is understood -- credentials,")
            print("                    symbol and real spread are all measured.")
            print("WHAT IT DOES NOT  : say anything about whether the strategy")
            print("                    makes money. That is a different question.")
            print()
            strat = [c for c in report.checks
                     if c.key in ("strategy_survived_falsification",
                                  "profit_not_outlier_driven",
                                  "cross_pair_breadth")]
            bad = [c for c in strat if not c.passed]
            if bad:
                print("STRATEGY STATUS (advisory, does NOT block the probe):")
                for c in bad:
                    print("   [!] %s" % c.detail)
                print()
                print("   => The strategy is NOT validated. A smoke order run now")
                print("      tests PLUMBING ONLY. Do not read its P&L as evidence.")
        else:
            print("NOT READY: %d blocking criteria failed." % len(report.blocking_failures))
            print()
            print("On the Windows MT5 host, run in this order:")
            print("    python -m scripts.fundingpips_connection_check")
            print("    python -m scripts.fundingpips_symbol_probe %s --save-profile" % args.symbol)
            print("    python -m scripts.fundingpips_record_spread --symbol %s --duration 3600" % args.symbol)

    if args.save:
        save_target = Path(args.save_path) if args.save_path else None
        path = save_readiness(report, path=save_target)
        if not args.json:
            print()
            print("Report saved to: %s" % path)

    return EXIT_OK if report.ready else EXIT_NOT_READY


if __name__ == "__main__":
    sys.exit(safe_main(run))
