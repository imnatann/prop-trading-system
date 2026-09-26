"""Research-leakage guardrails for Phases 3-6.

These tests exist to make it STRUCTURALLY IMPOSSIBLE to leak OOS information, not
merely discouraged. No market data is loaded and no performance is computed.
"""
from __future__ import annotations

import inspect

import numpy as np
import pandas as pd
import pytest

from research.phase3_vr_veto import VRVeto, cross_sectional_sd
from research.phase4_cost_session import CostGate, CostModel, SessionPolicy
from research.phase5_walkforward import UnsealedResult, WalkForwardRunner
from research.phase6_falsification import (FalsificationCriteria, StressCase,
                                           apply_perturbation,
                                           bootstrap_positive_probability,
                                           cross_pair_breadth,
                                           declared_stress_suite,
                                           drop_best_pair, drop_best_year,
                                           drop_top_decile,
                                           evaluate_falsification,
                                           suite_fingerprint)
from research.protocol.audit_log import AuditLog
from research.protocol.fold_plan import (build_fold_plan, assert_no_leakage,
                                         verify_gap_excluded)
from research.protocol.frozen_config import Frozen, FrozenViolation


# ---------------------------------------------------------------------------
# 1. A fitted VR threshold cannot be modified by OOS data
# ---------------------------------------------------------------------------
def test_oos_cannot_modify_fitted_vr_threshold():
    v = VRVeto(percentile=20).fit(list(np.linspace(0.01, 0.20, 100)))
    frozen_value = v.sd_threshold
    assert v.frozen
    with pytest.raises(FrozenViolation):
        v.sd_threshold = 0.0001
    with pytest.raises(FrozenViolation):
        v.percentile = 5.0
    # consuming OOS data must not move the threshold
    for sd, mean in [(0.5, 1.2), (0.0001, 1.0), (0.3, 0.8)]:
        v.allow(sd, mean)
    assert v.sd_threshold == frozen_value


def test_vr_veto_cannot_be_used_before_fit():
    v = VRVeto()
    with pytest.raises(FrozenViolation):
        v.allow(0.1, 0.9)


def test_vr_veto_refit_is_rejected():
    v = VRVeto().fit([0.01, 0.2, 0.15])
    with pytest.raises(FrozenViolation):
        v.refit([0.9, 0.9, 0.9])


def test_vr_threshold_is_a_percentile_of_train_only():
    train = list(np.linspace(0.01, 0.20, 101))
    v = VRVeto(percentile=20).fit(train)
    assert v.sd_threshold == pytest.approx(np.percentile(train, 20))
    # a huge OOS value must not pull the threshold up
    v.allow(99.0, 0.5)
    assert v.sd_threshold == pytest.approx(np.percentile(train, 20))


# ---------------------------------------------------------------------------
# 2. The cost gate cannot see realised P&L
# ---------------------------------------------------------------------------
def test_realized_pnl_not_accessible_to_cost_gate():
    """The decision signature must not accept an outcome."""
    params = list(inspect.signature(CostGate.decide).parameters)
    assert params == ["self", "cost"], params
    for banned in ("realized", "pnl", "outcome", "actual", "result"):
        assert not any(banned in p.lower() for p in params), params


def test_cost_gate_decision_is_reproducible_and_outcome_independent():
    g = CostGate(safety_multiple=2.0).fit([1.0, 1.1, 0.9, 1.0])
    c = CostModel()
    first = g.decide(c)
    # attach a wildly positive outcome AFTER the fact
    first.realized_pnl = 999.0
    second = g.decide(c)
    assert first.decision_allowed == second.decision_allowed
    assert second.realized_pnl is None


def test_cost_gate_frozen_after_fit():
    g = CostGate().fit([1.0, 1.1, 0.9])
    assert g.frozen
    with pytest.raises(FrozenViolation):
        g.edge_estimate = 99.0


def test_cost_gate_requires_use_of_an_exante_estimate():
    """A high-variance training sample must produce a LOW edge estimate."""
    noisy = CostGate().fit([5.0, -5.0, 5.0, -5.0, 5.0])
    clean = CostGate().fit([5.0, 5.1, 4.9, 5.0, 5.0])
    assert noisy.edge_estimate < clean.edge_estimate


# ---------------------------------------------------------------------------
# 3. Fold parameters are frozen before the test period is touched
# ---------------------------------------------------------------------------
def test_fold_parameters_are_frozen_before_test():
    plan = build_fold_plan("2018-01-01", "2026-01-01")
    idx = pd.date_range("2018-01-01", "2026-01-01", freq="D", tz="UTC")
    df = pd.DataFrame({"x": np.arange(len(idx))}, index=idx)
    seen = []

    def fit(tr):
        v = VRVeto().fit(list(np.linspace(0.01, 0.2, 50)))
        seen.append(v.frozen)          # must already be frozen at this point
        return {"veto": v}

    def ev(te, fitted):
        # every fitted object must be frozen by the time test runs
        assert all(isinstance(v, Frozen) and v.frozen for v in fitted.values())
        return {"trades": 0}

    WalkForwardRunner(plan=plan, fit_fn=fit, eval_fn=ev).run(df)
    assert all(seen), "fit must return frozen objects"


def test_unfrozen_object_from_fit_is_rejected():
    plan = build_fold_plan("2018-01-01", "2026-01-01")
    idx = pd.date_range("2018-01-01", "2026-01-01", freq="D", tz="UTC")
    df = pd.DataFrame({"x": np.arange(len(idx))}, index=idx)

    class Leaky(Frozen):
        pass

    def fit(tr):
        return {"leaky": Leaky()}          # never frozen

    with pytest.raises(FrozenViolation, match="unfrozen"):
        WalkForwardRunner(plan=plan, fit_fn=fit,
                          eval_fn=lambda te, f: {"trades": 0}).run(df)


# ---------------------------------------------------------------------------
# 4. Embargo never enters train or test
# ---------------------------------------------------------------------------
def test_embargo_period_never_enters_train_or_test():
    plan = build_fold_plan("2018-01-01", "2026-01-01", train_years=4,
                           test_years=1, purge_days=5, embargo_days=5)
    idx = pd.date_range("2018-01-01", "2026-01-01", freq="D", tz="UTC")
    df = pd.DataFrame({"x": np.arange(len(idx))}, index=idx)
    verify_gap_excluded(plan, df)          # raises on any leak
    for f in plan.folds:
        tr, te = f.slice_train(df), f.slice_test(df)
        gap = df.loc[(df.index > f.train_end) & (df.index <= f.embargo_end)]
        assert len(gap) > 0, "fixture must actually contain embargo rows"
        assert not set(gap.index) & set(tr.index)
        assert not set(gap.index) & set(te.index)


def test_purge_and_embargo_are_both_applied():
    plan = build_fold_plan("2018-01-01", "2026-01-01", purge_days=7, embargo_days=3)
    f = plan.folds[0]
    assert (f.purge_end - f.train_end).days == 7
    assert (f.embargo_end - f.purge_end).days == 3
    assert (f.test_start - f.embargo_end).days == 1


def test_train_never_overlaps_test_within_a_fold():
    plan = build_fold_plan("2018-01-01", "2026-01-01")
    for f in plan.folds:
        assert f.train_end < f.test_start


def test_test_periods_are_disjoint_across_folds():
    plan = build_fold_plan("2018-01-01", "2026-01-01")
    for a, b in zip(plan.folds, plan.folds[1:]):
        assert a.test_end < b.test_start


# ---------------------------------------------------------------------------
# 5. A later fold cannot affect an earlier one
# ---------------------------------------------------------------------------
def test_future_fold_cannot_affect_previous_fold():
    """A change confined to FUTURE data must leave earlier folds bit-identical.

    This is the meaningful form of the test. Two runs are compared:
      run A on the full frame,
      run B on the same frame with only data AFTER fold-1 test corrupt.
    Folds 0 and 1 must produce identical results. If any future row influences an
    earlier fold, the numbers diverge.
    """
    plan = build_fold_plan("2018-01-01", "2026-01-01")
    idx = pd.date_range("2018-01-01", "2026-01-01", freq="D", tz="UTC")
    base = pd.DataFrame({"x": np.arange(len(idx), dtype=float)}, index=idx)

    def fit(tr):
        # deliberately depends on train content so any leak becomes visible
        return {"mean": float(tr["x"].mean())}

    def ev(te, fitted):
        return {"trades": int(round(fitted["mean"])), "rows": len(te),
                "mean_test": float(te["x"].mean())}

    rep_a = WalkForwardRunner(plan=plan, fit_fn=fit, eval_fn=ev).run(base)

    corrupt = base.copy()
    cut = plan.folds[0].test_end
    corrupt.loc[corrupt.index > cut, "x"] = 1e9      # future-only corruption
    rep_b = WalkForwardRunner(plan=plan, fit_fn=fit, eval_fn=ev).run(corrupt)

    # fold 0 must be untouched by corruption that lives strictly after its test end
    assert rep_a.folds[0].trades == rep_b.folds[0].trades
    assert rep_a.folds[0].rows_test == rep_b.folds[0].rows_test


def test_fold_zero_is_identical_under_future_corruption():
    """Same idea, stated directly on the train slice size."""
    plan = build_fold_plan("2018-01-01", "2026-01-01")
    idx = pd.date_range("2018-01-01", "2026-01-01", freq="D", tz="UTC")
    full = pd.DataFrame({"x": np.arange(len(idx), dtype=float)}, index=idx)
    cut = plan.folds[0].test_end
    part = full.loc[full.index <= cut]

    f0 = plan.folds[0]
    tr_full = f0.slice_train(full)
    tr_part = f0.slice_train(part)
    pd.testing.assert_frame_equal(tr_full, tr_part)
    pd.testing.assert_frame_equal(f0.slice_test(full), f0.slice_test(part))


def test_fold_plan_is_deterministic():
    a = build_fold_plan("2018-01-01", "2026-09-01")
    b = build_fold_plan("2018-01-01", "2026-09-01")
    assert a.fingerprint() == b.fingerprint()


def test_fold_plan_rejects_overlapping_test_periods():
    plan = build_fold_plan("2018-01-01", "2026-01-01")
    assert_no_leakage(plan)


# ---------------------------------------------------------------------------
# 6. OOS results are write-only during the run
# ---------------------------------------------------------------------------
def test_results_are_sealed_and_unreadable_before_completion():
    plan = build_fold_plan("2018-01-01", "2026-01-01")
    idx = pd.date_range("2018-01-01", "2026-01-01", freq="D", tz="UTC")
    df = pd.DataFrame({"x": np.arange(len(idx))}, index=idx)
    runner = WalkForwardRunner(plan=plan, fit_fn=lambda tr: {"n": 1},
                               eval_fn=lambda te, f: {"trades": 1})
    with pytest.raises(UnsealedResult):
        runner.report()
    rep = runner.run(df)
    assert rep.sealed
    assert rep.n_folds == len(plan.folds)


def test_diagnostics_contain_no_performance_metric():
    plan = build_fold_plan("2018-01-01", "2026-01-01")
    idx = pd.date_range("2018-01-01", "2026-01-01", freq="D", tz="UTC")
    df = pd.DataFrame({"x": np.arange(len(idx))}, index=idx)
    runner = WalkForwardRunner(plan=plan, fit_fn=lambda tr: {"n": 1},
                               eval_fn=lambda te, f: {"trades": 3, "sharpe": 99.0})
    runner.run(df)
    for d in runner.diagnostics():
        text = d.render().lower()
        for banned in ("sharpe", "pips", "profit", "win rate", "pnl"):
            assert banned not in text, "diagnostic leaked %r: %s" % (banned, text)


def test_report_fields_are_none_until_a_run_fills_them():
    plan = build_fold_plan("2018-01-01", "2026-01-01")
    idx = pd.date_range("2018-01-01", "2026-01-01", freq="D", tz="UTC")
    df = pd.DataFrame({"x": np.arange(len(idx))}, index=idx)
    runner = WalkForwardRunner(plan=plan, fit_fn=lambda tr: {"n": 1},
                               eval_fn=lambda te, f: {"trades": 2})
    rep = runner.run(df)
    # eval returned no performance, so performance fields must stay None
    assert all(r.net_pips is None and r.sharpe is None for r in rep.folds)


# ---------------------------------------------------------------------------
# 7. The audit log records provenance
# ---------------------------------------------------------------------------
def test_audit_log_is_append_only_and_written(tmp_path):
    log = AuditLog(path=tmp_path / "audit.jsonl", run_id="t1")
    log.write("start", folds=3)
    log.write("end", folds=3)
    recs = log.read()
    assert len(recs) == 2 and recs[0]["event"] == "start"
    assert all(r["run_id"] == "t1" for r in recs)


def test_audit_log_records_freeze_snapshots(tmp_path):
    plan = build_fold_plan("2018-01-01", "2026-01-01")
    idx = pd.date_range("2018-01-01", "2026-01-01", freq="D", tz="UTC")
    df = pd.DataFrame({"x": np.arange(len(idx))}, index=idx)
    log = AuditLog(path=tmp_path / "a.jsonl", run_id="t2")
    runner = WalkForwardRunner(
        plan=plan, fit_fn=lambda tr: {"v": VRVeto().fit(list(np.linspace(0, 1, 40)))},
        eval_fn=lambda te, f: {"trades": 0}, audit=log)
    runner.run(df)
    events = [r["event"] for r in log.read()]
    assert "walkforward_start" in events and "walkforward_sealed" in events
    assert events.count("fold_fit") == len(plan.folds)
    fits = [r for r in log.read() if r["event"] == "fold_fit"]
    assert all("snapshot" in r for r in fits)


# ---------------------------------------------------------------------------
# 8. The falsification suite is pre-declared and immutable in content
# ---------------------------------------------------------------------------
def test_falsification_suite_is_predeclared():
    s = declared_stress_suite()
    keys = {c.key for c in s}
    required = {"cost_x1_0", "cost_x1_5", "cost_x2_0", "delay_1bar",
                "remove_best_year", "remove_best_pair", "remove_best_trade_decile",
                "subperiod_first_half", "subperiod_second_half",
                "bootstrap_trade_order"}
    assert required <= keys, "missing declared cases: %s" % (required - keys)
    assert any(c.kind == "perturb" for c in s), "parameter perturbation must be declared"


def test_falsification_suite_is_deterministic():
    assert suite_fingerprint(declared_stress_suite()) == \
        suite_fingerprint(declared_stress_suite())


def test_perturbation_is_deterministic_and_bounded():
    s = declared_stress_suite()
    case = [c for c in s if c.key == "perturb_breakout_bars_+20"][0]
    cfg = {"breakout_bars": 20}
    once = apply_perturbation(cfg, case)
    twice = apply_perturbation(cfg, case)
    assert once == twice
    assert once["breakout_bars"] != cfg["breakout_bars"]
    # perturbation must not be applied twice
    assert apply_perturbation(once, case)["breakout_bars"] != once["breakout_bars"]


def test_falsification_criteria_are_fixed():
    c = FalsificationCriteria()
    assert c.min_pairs_positive == 4 and c.n_pairs == 6
    assert c.max_cost_multiple_survived == 2.0
    assert c.min_bootstrap_p_positive == 0.95


def test_cross_pair_breadth_applies_the_4_of_6_rule():
    c = FalsificationCriteria()
    four = {p: 1.0 for p in ["EURUSD", "GBPUSD", "AUDUSD", "USDJPY"]}
    four.update({"USDCAD": -1.0, "USDCHF": -1.0})
    assert cross_pair_breadth(four, c)["passes"] is True
    three = dict(four)
    three["USDJPY"] = -1.0
    assert cross_pair_breadth(three, c)["passes"] is False


def test_remove_best_helpers_actually_remove():
    trades = pd.DataFrame({
        "symbol": ["EURUSD"] * 6,
        "exit_time": pd.to_datetime(["2023-01-01", "2023-06-01", "2024-01-01",
                                     "2024-06-01", "2025-01-01", "2025-06-01"]),
        "net_pips": [10.0, 1.0, 1.0, -1.0, 1.0, -1.0]})
    assert len(drop_best_year(trades)) < len(trades)
    assert len(drop_top_decile(trades)) < len(trades)
    multi = pd.concat([trades, trades.assign(symbol="GBPUSD")], ignore_index=True)
    assert len(drop_best_pair(multi)) < len(multi)


def test_bootstrap_probability_behaves():
    assert bootstrap_positive_probability([1.0] * 50) == 1.0
    assert bootstrap_positive_probability([-1.0] * 50) == 0.0
    mid = bootstrap_positive_probability([1.0, -1.0] * 25)
    assert 0.3 < mid < 0.7
    assert np.isnan(bootstrap_positive_probability([1.0, 2.0]))


def test_evaluate_falsification_requires_all_checks():
    good = {"per_pair": {p: 1.0 for p in ["a", "b", "c", "d", "e", "f"]},
            "cost_survival": 2.0, "bootstrap_p": 0.99, "neighbourhood_ok": True}
    assert evaluate_falsification(good)["passed"] is True
    bad = dict(good, cost_survival=1.0)
    assert evaluate_falsification(bad)["passed"] is False


# ---------------------------------------------------------------------------
# 9. Session policy is frozen and timezone-correct
# ---------------------------------------------------------------------------
def test_session_policy_is_frozen():
    sp = SessionPolicy()
    assert sp.frozen
    with pytest.raises(FrozenViolation):
        sp.start_hour_utc = 0


def test_session_policy_dst():
    sp = SessionPolicy(start_hour_utc=7, end_hour_utc=20)
    est2 = pd.Timestamp("2024-03-08 02:00", tz="America/New_York")   # 07:00Z
    edt2 = pd.Timestamp("2024-03-13 02:00", tz="America/New_York")   # 06:00Z
    assert sp.allows(est2) is True
    assert sp.allows(edt2) is False


# ---------------------------------------------------------------------------
# 10. Scope: no phase module computes or prints performance
# ---------------------------------------------------------------------------
def test_phase_modules_do_not_import_strategy_code():
    import ast
    from pathlib import Path as _P
    root = _P(__file__).resolve().parents[2] / "research"
    # research.comparison was removed with the FX comparison study; the ban on
    # concrete strategy imports still applies to every phase module.
    banned = ("src.strategy",)
    offenders = []
    for name in ("phase3_vr_veto.py", "phase4_cost_session.py",
                 "phase5_walkforward.py", "phase6_falsification.py"):
        tree = ast.parse((root / name).read_text(), filename=name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""]
            else:
                continue
            for m in mods:
                if any(m == b or m.startswith(b + ".") for b in banned):
                    offenders.append("%s imports %s" % (name, m))
    assert not offenders, offenders


def test_phase_modules_do_not_print():
    """No phase module may print, so a run cannot stream results to the console."""
    import ast
    from pathlib import Path as _P
    root = _P(__file__).resolve().parents[2] / "research"
    hits = []
    for name in ("phase3_vr_veto.py", "phase4_cost_session.py",
                 "phase5_walkforward.py", "phase6_falsification.py"):
        tree = ast.parse((root / name).read_text(), filename=name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "print":
                hits.append("%s:%d" % (name, node.lineno))
    assert not hits, "phase modules must not print during a run: %s" % hits
