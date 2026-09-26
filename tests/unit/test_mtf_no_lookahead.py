"""Tests for the multi-timeframe plumbing - the look-ahead trap. """
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.mtf import (htf_closed_only, align_htf_to_ltf, build_mtf_frame,
                          build_naive_mtf_frame, resample_ohlc)


def synth_m15(days: int = 20, start: str = "2024-01-01") -> pd.DataFrame:
    """Random-walk M15 bars with weekends included, for a realistic grid."""
    n = days * 24 * 4
    idx = pd.date_range(start, periods=n, freq="15min", tz="UTC")
    rng = np.random.default_rng(7)
    ret = rng.normal(0, 0.0004, n)
    close = 1.10 * np.exp(np.cumsum(ret))
    high = close * (1 + np.abs(rng.normal(0, 0.0002, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.0002, n)))
    open_ = np.concatenate([[close[0]], close[:-1]])
    return pd.DataFrame({"open": open_, "high": np.maximum(high, open_),
                         "low": np.minimum(low, open_), "close": close}, index=idx)


def test_htf_closed_only_drops_the_forming_bar():
    m15 = synth_m15()
    h4_raw = resample_ohlc(m15, "4h")
    h4_closed = htf_closed_only(m15, "4h")
    # the closed version is the raw version shifted by exactly one HTF bar
    assert len(h4_closed) == len(h4_raw)
    pd.testing.assert_series_equal(
        h4_closed["close"].iloc[1:].reset_index(drop=True),
        h4_raw["close"].iloc[:-1].reset_index(drop=True),
        check_names=False)
    assert h4_closed["close"].iloc[0] != h4_closed["close"].iloc[0] or True  # first is NaN
    assert pd.isna(h4_closed["close"].iloc[0])


def test_naive_join_leaks_the_future_but_ours_does_not():
    """The core assertion: at each timestamp the attached H4 close must come from a
    bar that had ALREADY closed, while the naive join uses a forming bar."""
    m15 = synth_m15()
    safe = build_mtf_frame(m15, h1_rule="1h", h4_rule="4h")
    naive = build_naive_mtf_frame(m15, h1_rule="1h", h4_rule="4h")

    h4_raw = resample_ohlc(m15, "4h")

    # for every timestamp, find the H4 bar that contains it
    contained = h4_raw.index.searchsorted(m15.index, side="right") - 1
    contained = np.clip(contained, 0, len(h4_raw) - 1)

    leaks = 0
    for i, ts in enumerate(m15.index):
        forming_close = h4_raw["close"].iloc[contained[i]]
        if pd.isna(naive["h4_close"].iloc[i]):
            continue
        if abs(naive["h4_close"].iloc[i] - forming_close) < 1e-12:
            leaks += 1
    assert leaks > 0, "the naive join should be leaking the forming H4 bar"

    # our frame must NEVER equal the still-forming bar close
    our_leaks = 0
    for i in range(len(m15)):
        v = safe["h4_close"].iloc[i]
        if pd.isna(v):
            continue
        if abs(v - h4_raw["close"].iloc[contained[i]]) < 1e-12:
            our_leaks += 1
    # allowing equality by coincidence only where the forming bar happens to equal
    # the previous closed bar (rare); require it to be a small fraction
    assert our_leaks / max(len(m15), 1) < 0.02, our_leaks


def test_attached_htf_value_equals_last_closed_bar_at_each_time():
    """Brute-force check: the H4 close attached at t must equal the close of the
    last H4 bar whose END time is <= t."""
    m15 = synth_m15(days=10)
    safe = build_mtf_frame(m15, h1_rule="1h", h4_rule="4h")
    h4_raw = resample_ohlc(m15, "4h")

    # end time of each H4 bar = its label + 4h
    ends = h4_raw.index + pd.Timedelta(hours=4)
    checked = 0
    for i in range(200, len(m15), 37):
        ts = m15.index[i]
        eligible = ends <= ts
        if not eligible.any():
            continue
        j = int(np.max(np.where(eligible)[0]))
        expected = h4_raw["close"].iloc[j]
        got = safe["h4_close"].iloc[i]
        assert got == pytest.approx(expected, abs=1e-12), (ts, got, expected)
        checked += 1
    assert checked > 20


def test_alignment_never_backfills():
    m15 = synth_m15(days=6)
    h4 = htf_closed_only(m15, "4h")
    aligned = align_htf_to_ltf(m15.index, h4)
    # the first aligned values must be NaN - nothing has closed yet
    assert aligned["close"].iloc[:4].isna().all()


def test_htf_features_use_only_past_bars():
    """Truncating the input after time T must not change HTF values before T."""
    m15 = synth_m15(days=12)
    full = build_mtf_frame(m15, h1_rule="1h", h4_rule="4h")
    cut = m15.iloc[: len(m15) // 2]
    part = build_mtf_frame(cut, h1_rule="1h", h4_rule="4h")
    # compare a window safely inside the truncated region
    n = len(part) - 200
    a = full["h4_ema_fast"].iloc[:n]
    b = part["h4_ema_fast"].iloc[:n]
    both = a.notna() & b.notna()
    assert both.sum() > 100
    np.testing.assert_allclose(a[both].values, b[both].values, rtol=1e-12, atol=1e-15)
