"""Operator CLI: run a REAL-data walk-forward against the FundingPips rules.

    python -m scripts.fundingpips_research fetch --symbol EURUSD --range 10y
    python -m scripts.fundingpips_research run --symbol EURUSD
    python -m scripts.fundingpips_research stress --symbol EURUSD

This tool places NO orders and needs NO credentials. It is pure research and
runs on any platform, including macOS/Linux where MetaTrader5 does not exist.

What it is NOT
--------------
It does not validate a strategy as profitable. It reports survival and
pass-rate statistics for a FIXED parameter set over REAL bars, so that a
hypothesis can be rejected cheaply before anything is risked.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from research.backtest.cross_sectional import (
    MIN_POSITIVE_PAIRS,
    PROTOCOL_UNIVERSE,
    cross_sectional_walk_forward,
)
from research.backtest.falsify_real import falsify_walk_forward
from research.backtest.grid_daily import (
    DECLARED_DAILY_GRID,
    run_grid,
)
from research.backtest.walkforward_real import (
    StrategyParams,
    build_folds,
    cost_stress,
    walk_forward,
)
from research.data.real_feed import (
    INTERVAL_LIMITS,
    fetch_yahoo,
    load_csv,
    quality_report,
    save_csv,
    save_provenance,
)
from config.fundingpips_rules import MODELS, RULES_AS_OF, TWO_STEP_STANDARD

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_NO_DATA = 2

from src.paths import REAL_DATA_DIR as DATA_DIR


def _path(symbol: str, interval: str, data_dir: Optional[str] = None) -> Path:
    return Path(data_dir or DATA_DIR) / ("%s_%s.csv" % (symbol.upper(), interval))


def cmd_fetch(args) -> int:
    print("Fetching %s %s (%s) from Yahoo Finance..." % (args.symbol, args.interval, args.range))
    candles, meta = fetch_yahoo(args.symbol, args.interval, args.range)
    q = quality_report(candles)
    print()
    print("Bars              : %d" % q["bars"])
    print("Span              : %s -> %s  (%.2f years)"
          % (q["first_utc"][:10], q["last_utc"][:10], q["span_years"]))
    print("Crossed bars      : %d" % q["crossed_bars"])
    print("Gaps over 4 days  : %d (max %.0f)" % (q["gaps_over_4_days"], q["max_gap_days"]))
    print("Problems          : %s" % (q["problems"] or "none"))
    if not q["ok"]:
        print()
        print("DATA FAILED QUALITY CHECKS. Not saving.")
        return EXIT_FAILURE

    csv_path = save_csv(candles, args.symbol, args.interval)
    prov = save_provenance(meta, q, args.symbol, args.interval)
    print()
    print("Saved   : %s" % csv_path)
    print("Sidecar : %s" % prov)
    print()
    print("REMINDER: this is indicative OHLC, not executable quotes.")
    print("Spread is MODELLED. Verify with the Free Trial before any paid eval.")
    return EXIT_OK


def _load_or_fetch(args):
    p = _path(args.symbol, args.interval, args.data_dir)
    if p.exists():
        return load_csv(p), False
    print("No cached file at %s - fetching..." % p)
    candles, meta = fetch_yahoo(args.symbol, args.interval, args.range)
    q = quality_report(candles)
    if not q["ok"]:
        print("DATA FAILED QUALITY CHECKS:", q["problems"])
        return None, False
    save_csv(candles, args.symbol, args.interval)
    save_provenance(meta, q, args.symbol, args.interval)
    return candles, True


def _params(args) -> StrategyParams:
    return StrategyParams(
        fast=args.fast, slow=args.slow,
        stop_pips=args.stop_pips, target_pips=args.target_pips,
        lots=args.lots,
    )


def cmd_run(args) -> int:
    candles, _ = _load_or_fetch(args)
    if not candles:
        return EXIT_NO_DATA
    model = MODELS.get(args.model, TWO_STEP_STANDARD)

    folds = build_folds(candles, args.train_years, args.test_years, args.embargo_days)
    print()
    print("=" * 74)
    print("REAL-DATA WALK-FORWARD  |  %s  |  %s daily bars" % (args.symbol, len(candles)))
    print("Model: %s (%s)  rules as of %s" % (model.label, model.key, RULES_AS_OF))
    print("=" * 74)
    print("Folds: %d   (train %dy / test %dy / embargo %dd)"
          % (len(folds), args.train_years, args.test_years, args.embargo_days))
    for f in folds:
        print("   fold %d  TEST %s .. %s" % (f.index, f.test_start.date(), f.test_end.date()))
    if not folds:
        print("NOT ENOUGH DATA to build a single fold.")
        return EXIT_NO_DATA

    res = walk_forward(candles, _params(args), model,
                       initial_balance=args.balance,
                       spread_pips=args.spread, slippage_pips=args.slippage,
                       train_years=args.train_years, test_years=args.test_years,
                       embargo_days=args.embargo_days)
    s = res.summary()

    print()
    print("Params (frozen across folds): %s" % json.dumps(s["params"], sort_keys=True))
    print("Assumed spread %.2f pip, slippage %.2f pip" % (args.spread, args.slippage))
    print()
    print("%-6s %8s %8s %9s %12s %14s %12s" %
          ("fold", "trades", "passed", "survived", "final_bal", "maxDD%", "breach"))
    for d in s["detail"]:
        print("%-6d %8d %8s %9s %12.0f %13.2f%% %12s"
              % (d["fold"], d["trades"], d["passed"], d["survived"],
                 d["final_balance"], d["max_total_loss_pct"], d["breach"]))
    print()
    print("PASS RATE     : %.1f%%  (%d/%d folds)" % (100*s["pass_rate"], s["passed_folds"], s["folds"]))
    print("SURVIVAL RATE : %.1f%%  (%d/%d folds)" % (100*s["survival_rate"], s["survived_folds"], s["folds"]))
    print()
    print("A breach is an account LOSS, not a drawdown warning: the evaluation ends.")
    return EXIT_OK


def cmd_stress(args) -> int:
    candles, _ = _load_or_fetch(args)
    if not candles:
        return EXIT_NO_DATA
    model = MODELS.get(args.model, TWO_STEP_STANDARD)
    rows = cost_stress(candles, _params(args), model,
                       initial_balance=args.balance,
                       base_spread=args.spread, base_slip=args.slippage)

    print()
    print("=" * 74)
    print("COST STRESS  |  same FROZEN params, escalating costs")
    print("=" * 74)
    print("%-10s %9s %11s %7s %10s %12s %9s"
          % ("stress", "spread", "slippage", "folds", "survived", "survival%", "pass%"))
    for r in rows:
        print("%-10s %9.2f %11.2f %7d %10d %11.1f%% %8.1f%%"
              % (r["stress"], r["spread_pips"], r["slippage_pips"], r["folds"],
                 r["survived"], 100*r["survival_rate"], 100*r["pass_rate"]))
    print()
    print("Read this honestly: if pass-rate collapses as costs rise, the edge was")
    print("a cost assumption. If it is FLAT, costs are too small to matter at")
    print("this position size - which is its own warning, not a good sign.")
    return EXIT_OK


def cmd_falsify(args) -> int:
    """Run the pre-declared Phase 6 suite against REAL trades.

    This is the step that decides whether a result deserves to be believed. It
    deliberately tries to KILL the strategy rather than confirm it.
    """
    candles, _ = _load_or_fetch(args)
    if not candles:
        return EXIT_NO_DATA
    model = MODELS.get(args.model, TWO_STEP_STANDARD)

    res = walk_forward(candles, _params(args), model,
                       initial_balance=args.balance,
                       spread_pips=args.spread, slippage_pips=args.slippage,
                       train_years=args.train_years, test_years=args.test_years,
                       embargo_days=args.embargo_days)
    report = falsify_walk_forward(res)

    print()
    print("=" * 74)
    print("PHASE 6 FALSIFICATION  |  %s  |  declared suite, not adjusted" % args.symbol)
    print("=" * 74)
    print("Suite fingerprint: %s" % report.suite_fingerprint)
    print("Trades: %d over %d folds" % (report.n_trades, report.n_folds))
    print()
    print(report.render())
    print()
    print("VERDICT: %s" % report.verdict)
    if report.verdict == "FALSIFIED":
        print()
        print("The result did NOT survive. That is a useful outcome: it cost")
        print("nothing and it saved an account. Do NOT proceed to a smoke order")
        print("on the strength of this parameter set.")
    elif report.verdict == "UNDERPOWERED":
        print()
        print("Too few trades to make any claim either way.")
    else:
        print()
        print("Survived the declared attacks. This is NOT proof of profit; it")
        print("means the idea has not yet been falsified.")
    return EXIT_OK


def cmd_cross(args) -> int:
    """Run the SAME frozen parameters across the whole protocol universe.

    One pair proves nothing. The purpose of the universe is to separate
    "this is an FX phenomenon" from "this was one pair in one period".
    """
    import json as _json

    rep = cross_sectional_walk_forward(
        params=_params(args), model=MODELS.get(args.model, TWO_STEP_STANDARD),
        initial_balance=args.balance,
        spread_pips=args.spread, slippage_pips=args.slippage,
    )
    s = rep.summary()

    print()
    print("=" * 82)
    print("CROSS-SECTIONAL WALK-FORWARD | same frozen params on every pair")
    print("=" * 82)
    print()
    print("%-9s %6s %7s %9s %11s %9s %7s %-13s" % (
        "pair", "folds", "passed", "survived", "total_net", "expect", "PF",
        "falsif"))
    print("-" * 82)
    for d in s["detail"]:
        print("%-9s %6d %7d %9d %11.0f %9.2f %7.2f %-13s" % (
            d["symbol"], d["folds"], d["passed_folds"], d["survived_folds"],
            d["total_net"], d["expectancy_pips"], d["profit_factor"],
            d["falsification"]))
    print("-" * 82)
    print()
    print("positive pairs      : %d / %d  (protocol needs >= %d)" % (
        s["positive_pairs"], s["pairs_evaluated"], MIN_POSITIVE_PAIRS))
    print("breadth passes      : %s" % s["breadth_passes"])
    print("any survived falsif : %s" % s["any_survived_falsification"])
    if s["pairs_missing"]:
        print("missing pairs       : %s" % ", ".join(s["pairs_missing"]))
    print()
    print("VERDICT: %s" % s["verdict"])
    print()
    print("A single surviving pair out of six is what LUCK looks like, not an")
    print("edge. With 6 tests the best is expected to look good by chance alone.")
    return EXIT_OK


def cmd_grid(args) -> int:
    """Evaluate the DECLARED grid and report every configuration.

    Reporting only the winner is the exact failure mode the protocol exists to
    prevent, so all rows are printed.
    """
    rep = run_grid(balance=args.balance, spread=args.spread,
                   slippage=args.slippage)
    s = rep.summary()

    print()
    print("=" * 84)
    print("DECLARED DAILY GRID | %d configs x 6 pairs, every config reported"
          % s["n_configs"])
    print("=" * 84)
    print()
    print("%-4s %-26s %7s %11s %8s %8s %-11s" % (
        "cfg", "params", "pos/6", "total_net", "expect", "survived", "verdict"))
    print("-" * 84)
    for d in s["results"]:
        p = d["params"]
        ps = "f%d/s%d st%.0f tg%.0f" % (p["fast"], p["slow"],
                                        p["stop_pips"], p["target_pips"])
        print("%-4s %-26s %5d/%-2d %11.0f %8.2f %5d/6 %-11s" % (
            d["label"], ps, d["pairs_positive"], d["pairs_total"],
            d["total_net"], d["expectancy_pips"],
            d["survived_falsification"], d["verdict"]))
    print("-" * 84)
    print()
    print("configs tested   : %d" % s["n_configs"])
    print("luck hurdle      : Sharpe %.3f (for %d configs)"
          % (s["luck_hurdle_sharpe"], s["n_configs"]))
    print("breadth reached  : %d of %d configs"
          % (sum(1 for d in s["results"] if d["breadth_passes"]), s["n_configs"]))
    print("any candidate    : %s" % s["any_candidate"])
    print()
    print("WARNING: a grid search over real data is still a search. Several")
    print("configurations WILL look good. Only breadth AND survival together")
    print("separate a finding from a coincidence.")
    return EXIT_OK


def cmd_limits(args) -> int:
    print("Data availability measured 2026-09-25 (Yahoo Finance):")
    print()
    for k, v in INTERVAL_LIMITS.items():
        print("   %-5s %s" % (k, v))
    print()
    print("The SMALLEST timeframe is the binding constraint. Multi-year M15 is")
    print("NOT available from this feed, so the H4/H1/M15 hypothesis cannot be")
    print("tested here. Daily is.")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fundingpips_research",
        description="Real-data research against the FundingPips rules (no orders, no credentials).")
    sub = p.add_subparsers(dest="command", required=True)

    common = dict()
    for name, fn, helptext in (
        ("fetch", cmd_fetch, "download and validate bars"),
        ("run", cmd_run, "walk-forward over real bars"),
        ("stress", cmd_stress, "cost-stress the same frozen params"),
        ("falsify", cmd_falsify, "run the pre-declared falsification suite"),
        ("cross", cmd_cross, "same params across the 6-pair protocol universe"),
        ("grid", cmd_grid, "evaluate the DECLARED grid; report every config"),
        ("limits", cmd_limits, "show what data is actually available"),
    ):
        sp = sub.add_parser(name, help=helptext)
        sp.set_defaults(func=fn)
        if name != "limits":
            sp.add_argument("--symbol", default="EURUSD")
            sp.add_argument("--interval", default="1d")
            sp.add_argument("--range", dest="range", default="10y")
            sp.add_argument("--data-dir", default=None)
        if name in ("run", "stress", "falsify", "cross", "grid"):
            sp.add_argument("--model", default="2_step_standard",
                            choices=sorted(MODELS.keys()))
            sp.add_argument("--balance", type=float, default=10000.0)
            sp.add_argument("--spread", type=float, default=1.0)
            sp.add_argument("--slippage", type=float, default=0.3)
            sp.add_argument("--fast", type=int, default=20)
            sp.add_argument("--slow", type=int, default=100)
            sp.add_argument("--stop-pips", type=float, default=60.0)
            sp.add_argument("--target-pips", type=float, default=120.0)
            sp.add_argument("--lots", type=float, default=0.10)
            sp.add_argument("--train-years", type=int, default=4)
            sp.add_argument("--test-years", type=int, default=1)
            sp.add_argument("--embargo-days", type=int, default=30)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print()
        print("Interrupted.")
        return EXIT_OK
    except Exception as exc:  # CLI boundary: never leak a raw traceback
        print()
        print("FAILED: %s: %s" % (type(exc).__name__, str(exc)[:300]))
        return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
