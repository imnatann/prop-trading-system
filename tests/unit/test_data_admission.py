"""Unit tests for the data admission gate."""
from __future__ import annotations

import pandas as pd
import pytest

from research.data_admission import (Coverage, PAIRS, TIMEFRAMES, common_window,
                                     admit_for_walk_forward)


def cov(pair, tf, start, end, bars=10000):
    return Coverage(pair, tf, pd.Timestamp(start, tz="UTC"),
                    pd.Timestamp(end, tz="UTC"), bars)


def full(pair, start="2015-01-01", end="2026-01-01"):
    return [cov(pair, tf, start, end) for tf in TIMEFRAMES]


def test_admits_when_all_pairs_and_timeframes_present():
    covs = [c for p in PAIRS for c in full(p)]
    r = common_window(covs)
    assert r.admitted
    assert r.window_start == pd.Timestamp("2015-01-01", tz="UTC")
    assert r.window_end == pd.Timestamp("2026-01-01", tz="UTC")


def test_window_is_the_INTERSECTION_not_the_widest_series():
    """The exact scenario from the review: EURUSD from 2014 but AUDUSD only from 2018
    must yield a 2018 start, not 2014."""
    covs = (full("EURUSD", "2014-01-01", "2026-01-01")
            + full("GBPUSD", "2015-01-01", "2026-01-01")
            + full("USDJPY", "2013-01-01", "2026-01-01")
            + full("AUDUSD", "2018-01-01", "2026-01-01")
            + full("USDCAD", "2017-01-01", "2026-01-01")
            + full("USDCHF", "2016-01-01", "2026-01-01"))
    r = common_window(covs)
    assert r.admitted
    assert r.window_start == pd.Timestamp("2018-01-01", tz="UTC"), \
        "common window must start at the LATEST first-available date"
    assert ("AUDUSD", "M15") in r.binding


def test_latest_end_is_governed_by_the_oldest_series():
    covs = ([c for p in PAIRS if p != "USDCHF" for c in full(p, end="2026-01-01")]
            + full("USDCHF", end="2024-01-01"))
    r = common_window(covs)
    assert r.window_end == pd.Timestamp("2024-01-01", tz="UTC")


def test_missing_series_blocks_admission():
    covs = [c for p in PAIRS if p != "USDCAD" for c in full(p)]
    r = common_window(covs)
    assert not r.admitted
    assert ("USDCAD", "M15") in r.missing
    assert "NOT ADMITTED" in r.summary()


def test_missing_timeframe_blocks_admission():
    """Having M15 and H1 but not H4 must fail: the hypothesis needs all three."""
    covs = []
    for p in PAIRS:
        for tf in ("M15", "H1"):
            covs.append(cov(p, tf, "2015-01-01", "2026-01-01"))
    r = common_window(covs)
    assert not r.admitted
    assert ("EURUSD", "H4") in r.missing


def test_empty_series_is_treated_as_missing():
    covs = [c for p in PAIRS for c in full(p)]
    covs = [c for c in covs if not (c.pair == "GBPUSD" and c.timeframe == "H1")]
    covs.append(Coverage("GBPUSD", "H1", None, None, 0))
    r = common_window(covs)
    assert not r.admitted
    assert ("GBPUSD", "H1") in r.missing


def test_non_overlapping_series_are_rejected():
    covs = [c for p in PAIRS if p != "AUDUSD" for c in full(p, "2015-01-01", "2020-01-01")]
    covs += full("AUDUSD", "2021-01-01", "2026-01-01")
    r = common_window(covs)
    assert not r.admitted


def test_walk_forward_depth_requirement_rejects_shallow_history():
    """6 years is not enough for train=4y plus 3 one-year folds (needs 7y)."""
    covs = [c for p in PAIRS for c in full(p, "2020-01-01", "2026-01-01")]
    r = common_window(covs)
    assert r.admitted, "the raw coverage is complete"
    r = admit_for_walk_forward(r, train_years=4, test_years=1, min_folds=3)
    assert not r.admitted, "6 years cannot support 4y train + 3 folds"
    assert any("required for" in n for n in r.notes)


def test_walk_forward_requirement_passes_with_enough_depth():
    covs = [c for p in PAIRS for c in full(p, "2015-01-01", "2026-01-01")]
    r = common_window(covs)
    r = admit_for_walk_forward(r, train_years=4, test_years=1, min_folds=3)
    assert r.admitted
    assert any("supports >=" in n for n in r.notes)
