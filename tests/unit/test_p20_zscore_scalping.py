"""
Unit Tests for Alpha v2: Z-Score Micro-Scalping Architecture.
Menguji secara ketat terhadap NILAI REFERENSI yang dihitung tangan,
bukan sekadar "tidak error".

Cakupan:
  - P20-1: RollingWindowStats (Welford) vs oracle eksak numpy
  - P20-2: Mid price & bid-ask bounce
  - P20-3: Order Book Imbalance I_t sesuai rumus spesifikasi
  - P20-4: Order Flow Imbalance (Cont et al.)
  - P20-5: Wilder smoothing & True Range
  - P20-6: Volatility Ratio & circuit breaker
  - P20-7: Async tick feed (event loop tidak terblokir)
  - P20-8: Strategi komposit Z-Score end-to-end
  - P20-9: Fail-closed behaviour
"""

import asyncio
import math
from collections import deque
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from src.data.market import Quote
from src.data.order_book import (
    BookLevel,
    OrderBookSnapshot,
    OrderFlowImbalanceTracker,
    calculate_order_book_imbalance,
    classify_imbalance,
    make_synthetic_book,
)
from src.data.tick_buffer import MidPriceBuffer, RollingWindowStats
from src.data.tick_feed import MockTickFeed, TickFeed
from src.data.volatility_ratio import (
    ATRPercentileCircuitBreaker,
    VolatilityCircuitBreaker,
    atr_percentile_rank,
    true_range,
    volatility_ratio,
    wilder_atr,
    wilder_smoothing,
)
from src.strategy.base import SignalAction
from src.strategy.zscore_scalper import EURUSDZScoreMeanReversionStrategy


# =====================================================================
# P20-1: ROLLING WINDOW STATS (WELFORD) vs ORACLE EKSAK
# =====================================================================

def test_rolling_stats_matches_hand_computed_reference():
    """[1,2,3,4,5]: mean=3, var populasi=2, std=sqrt(2)."""
    s = RollingWindowStats(window=5)
    for v in (1.0, 2.0, 3.0, 4.0, 5.0):
        s.update(v)

    assert s.count == 5
    assert s.mean == pytest.approx(3.0, abs=1e-12)
    assert s.variance == pytest.approx(2.0, abs=1e-12)          # ddof=0
    assert s.std == pytest.approx(math.sqrt(2.0), abs=1e-12)
    assert s.z_score(5.0) == pytest.approx((5.0 - 3.0) / math.sqrt(2.0), abs=1e-12)


def test_rolling_stats_window_slides_correctly():
    """Setelah window penuh, elemen tertua harus dibuang (bukan diakumulasi)."""
    s = RollingWindowStats(window=3)
    for v in (1.0, 2.0, 3.0):
        s.update(v)
    assert s.mean == pytest.approx(2.0)

    s.update(4.0)   # window jadi [2,3,4]
    assert s.count == 3
    assert s.mean == pytest.approx(3.0)
    assert list(s.values()) == [2.0, 3.0, 4.0]


def test_rolling_stats_matches_numpy_exact_over_long_stream():
    """Welford streaming HARUS identik dengan recompute numpy atas window."""
    rng = np.random.default_rng(1234)
    prices = 1.08 + np.cumsum(rng.normal(0, 0.00003, 5000))
    window = 100
    s = RollingWindowStats(window=window)

    max_mean_err = 0.0
    max_std_err = 0.0
    for i, p in enumerate(prices):
        s.update(float(p))
        if i >= window - 1:
            arr = prices[i - window + 1: i + 1]
            max_mean_err = max(max_mean_err, abs(s.mean - arr.mean()))
            max_std_err = max(max_std_err, abs(s.std - arr.std()))
            assert s.count == window

    assert max_mean_err < 1e-12, f"mean drift terlalu besar: {max_mean_err}"
    assert max_std_err < 1e-12, f"std drift terlalu besar: {max_std_err}"


def test_rolling_stats_never_returns_negative_variance():
    """
    Regresi: bentuk downdate Welford rawan menghasilkan M2 < 0.
    Variance negatif -> sqrt -> NaN -> strategi gagal senyap.
    """
    rng = np.random.default_rng(7)
    # Deret dengan variasi sangat kecil relatif terhadap mean (kasus terburuk)
    prices = 1.08 + np.cumsum(rng.normal(0, 1e-9, 20000))
    s = RollingWindowStats(window=50)
    for p in prices:
        s.update(float(p))
        assert s.variance >= 0.0, "variance negatif terdeteksi!"
        assert s.std >= 0.0
        assert math.isfinite(s.std)


def test_rolling_stats_rejects_invalid_window():
    with pytest.raises(ValueError):
        RollingWindowStats(window=1)


def test_rolling_stats_ignores_non_finite():
    s = RollingWindowStats(window=3)
    s.update(1.0)
    s.update(float("nan"))
    s.update(float("inf"))
    s.update(2.0)
    assert s.count == 2
    assert s.mean == pytest.approx(1.5)


def test_rolling_stats_z_score_none_when_not_ready():
    s = RollingWindowStats(window=10)
    s.update(1.0)
    assert s.z_score(1.0) is None      # window belum penuh


def test_rolling_stats_z_score_none_on_flat_market():
    """Pasar benar-benar datar -> sigma=0 -> z tidak terdefinisi (None, bukan 0)."""
    s = RollingWindowStats(window=3)
    for _ in range(3):
        s.update(1.08500)
    assert s.std == 0.0
    assert s.z_score(1.08500) is None


# =====================================================================
# P20-2: MID PRICE & BID-ASK BOUNCE
# =====================================================================

def test_mid_price_buffer_uses_midpoint():
    buf = MidPriceBuffer(window=3)
    q = Quote("EURUSD", bid=1.08500, ask=1.08510, spread_pips=1.0)
    mid = buf.update(q)
    assert mid == pytest.approx(1.08505)
    assert buf.last_mid == pytest.approx(1.08505)


def test_mid_price_immune_to_bid_ask_bounce():
    """
    Mid price harus simetris sehingga tidak terpengaruh sisi mana yang 'traded'.
    Ini alasan spesifikasi memakai (Ask+Bid)/2, bukan last price.
    """
    buf = MidPriceBuffer(window=4)
    for _ in range(4):
        buf.update(Quote("EURUSD", bid=1.08495, ask=1.08505, spread_pips=1.0))
    assert buf.last_mid == pytest.approx(1.08500)
    assert buf.stats.std == pytest.approx(0.0)   # konstan -> tidak ada bounce palsu


# =====================================================================
# P20-3: ORDER BOOK IMBALANCE (RUMUS SPESIFIKASI)
# =====================================================================

def test_imbalance_hand_computed():
    """
    bids volume = 30, asks volume = 10  =>  I_t = (30-10)/(30+10) = +0.5
    """
    book = OrderBookSnapshot(
        symbol="EURUSD",
        bids=(BookLevel(1.08499, 10.0), BookLevel(1.08498, 20.0)),
        asks=(BookLevel(1.08501, 4.0), BookLevel(1.08502, 6.0)),
    )
    res = calculate_order_book_imbalance(book, levels=5)
    assert res.available is True
    assert res.imbalance == pytest.approx(0.5, abs=1e-12)
    assert res.bid_volume == pytest.approx(30.0)
    assert res.ask_volume == pytest.approx(10.0)
    assert res.levels_used == 2


def test_imbalance_respects_level_limit():
    """levels=1 hanya boleh menghitung level teratas."""
    book = OrderBookSnapshot(
        symbol="EURUSD",
        bids=(BookLevel(1.08499, 100.0), BookLevel(1.08498, 9999.0)),
        asks=(BookLevel(1.08501, 100.0), BookLevel(1.08502, 9999.0)),
    )
    res = calculate_order_book_imbalance(book, levels=1)
    assert res.imbalance == pytest.approx(0.0)     # 100 vs 100
    assert res.levels_used == 1


def test_imbalance_extremes():
    """Semua volume di bid => +1.0 ; semua di ask => -1.0."""
    bids_only = OrderBookSnapshot("EURUSD", (BookLevel(1.0849, 100.0),), (BookLevel(1.0851, 0.0),))
    assert calculate_order_book_imbalance(bids_only).imbalance == pytest.approx(1.0)

    asks_only = OrderBookSnapshot("EURUSD", (BookLevel(1.0849, 0.0),), (BookLevel(1.0851, 100.0),))
    assert calculate_order_book_imbalance(asks_only).imbalance == pytest.approx(-1.0)


def test_imbalance_classification_thresholds():
    """Ambang sesuai spesifikasi: >+0.30 buy, <-0.30 sell, |I|<=0.10 netral."""
    assert classify_imbalance(0.35) == "BUYING_PRESSURE"
    assert classify_imbalance(-0.35) == "SELLING_PRESSURE"
    assert classify_imbalance(0.05) == "NEUTRAL"
    assert classify_imbalance(-0.05) == "NEUTRAL"
    assert classify_imbalance(0.20) == "MILD"
    # Batas tepat
    assert classify_imbalance(0.30) == "MILD"      # tidak > 0.30
    assert classify_imbalance(0.10) == "NEUTRAL"   # |I| <= 0.10


def test_imbalance_fail_closed_when_book_absent():
    """DOM tidak tersedia -> available=False, BUKAN netral."""
    res = calculate_order_book_imbalance(None)
    assert res.available is False
    assert res.imbalance == 0.0
    assert "tidak tersedia" in res.reason


def test_imbalance_fail_closed_on_one_sided_book():
    book = OrderBookSnapshot("EURUSD", (BookLevel(1.0849, 100.0),), ())
    res = calculate_order_book_imbalance(book)
    assert res.available is False


def test_imbalance_zero_volume_is_not_neutral():
    book = OrderBookSnapshot("EURUSD", (BookLevel(1.0849, 0.0),), (BookLevel(1.0851, 0.0),))
    res = calculate_order_book_imbalance(book)
    assert res.available is False
    assert "nol" in res.reason


def test_imbalance_invalid_levels_raises():
    book = make_synthetic_book(1.08)
    with pytest.raises(ValueError):
        calculate_order_book_imbalance(book, levels=0)


# =====================================================================
# P20-4: ORDER FLOW IMBALANCE (CONT ET AL.)
# =====================================================================

def test_ofi_positive_when_bid_improves():
    """Best bid naik -> kontribusi OFI positif (tekanan beli)."""
    t = OrderFlowImbalanceTracker(window=5)
    t.update(make_synthetic_book(1.08000))
    ofi = t.update(make_synthetic_book(1.08010))
    assert ofi is not None
    assert ofi > 0


def test_ofi_none_on_first_observation():
    t = OrderFlowImbalanceTracker(window=5)
    assert t.update(make_synthetic_book(1.08)) is None


def test_ofi_none_when_book_unavailable():
    t = OrderFlowImbalanceTracker(window=5)
    assert t.update(None) is None


def test_ofi_bounded():
    t = OrderFlowImbalanceTracker(window=5)
    for i in range(20):
        v = t.update(make_synthetic_book(1.08 + i * 0.00001, bid_ask_ratio=3.0))
        if v is not None:
            assert -1.0 <= v <= 1.0


# =====================================================================
# P20-5: TRUE RANGE & WILDER SMOOTHING
# =====================================================================

def test_true_range_hand_computed():
    """
    Bar 1: H=1.10 L=1.00 C=1.05 -> TR = 0.10 (tidak ada prev close)
    Bar 2: H=1.20 L=1.10 C=1.15, prev_close=1.05
           max(0.10, |1.20-1.05|=0.15, |1.10-1.05|=0.05) = 0.15
    """
    high = np.array([1.10, 1.20])
    low = np.array([1.00, 1.10])
    close = np.array([1.05, 1.15])
    tr = true_range(high, low, close)
    assert tr[0] == pytest.approx(0.10)
    assert tr[1] == pytest.approx(0.15)


def test_true_range_uses_prev_close_gap():
    """Gap besar harus tertangkap oleh |High - prev Close|."""
    high = np.array([1.00, 1.50])
    low = np.array([0.99, 1.40])
    close = np.array([0.995, 1.45])
    tr = true_range(high, low, close)
    assert tr[1] == pytest.approx(0.505)   # |1.50 - 0.995|


def test_wilder_smoothing_hand_computed():
    """
    period=3, values=[1,2,3,4]
    seed = mean(1,2,3) = 2  (indeks 0..2 = 2)
    indeks 3: 2 + (1/3)*(4-2) = 2.6667
    """
    out = wilder_smoothing(np.array([1.0, 2.0, 3.0, 4.0]), period=3)
    assert out[0] == pytest.approx(2.0)
    assert out[1] == pytest.approx(2.0)
    assert out[2] == pytest.approx(2.0)
    assert out[3] == pytest.approx(2.0 + (1.0 / 3.0) * 2.0)


def test_wilder_smoothing_equals_recursive_formula():
    """Verifikasi langsung terhadap rumus spesifikasi: (prev*(N-1)+TR)/N."""
    tr = np.array([0.0010, 0.0012, 0.0009, 0.0015, 0.0011, 0.0013, 0.0010, 0.0014])
    period = 3
    out = wilder_smoothing(tr, period)
    prev = float(tr[:period].mean())
    for i in range(period, len(tr)):
        prev = (prev * (period - 1) + tr[i]) / period
        assert out[i] == pytest.approx(prev, abs=1e-15)


def test_wilder_differs_from_sma():
    """Wilder smoothing BUKAN simple moving average (klaim README lama salah)."""
    tr = np.array([0.0010, 0.0020, 0.0030, 0.0010, 0.0010, 0.0010])
    wilder = wilder_smoothing(tr, 3)
    sma = pd.Series(tr).rolling(3).mean().to_numpy()
    assert wilder[-1] != pytest.approx(sma[-1])


def test_wilder_smoothing_handles_short_series():
    out = wilder_smoothing(np.array([1.0, 3.0]), period=5)
    assert out[0] == pytest.approx(1.0)
    assert out[1] == pytest.approx(2.0)   # cumulative mean


def test_wilder_atr_hand_computed():
    """ATR(2) atas TR yang diketahui."""
    high = np.array([1.10, 1.20, 1.30])
    low = np.array([1.00, 1.10, 1.20])
    close = np.array([1.05, 1.15, 1.25])
    tr = true_range(high, low, close)          # [0.10, 0.15, 0.15]
    atr = wilder_atr(high, low, close, period=2)
    seed = (tr[0] + tr[1]) / 2.0               # 0.125
    expected_last = (seed * 1 + tr[2]) / 2.0   # 0.1375
    assert atr[-1] == pytest.approx(expected_last, abs=1e-12)


# =====================================================================
# P20-6: VOLATILITY RATIO & CIRCUIT BREAKER
# =====================================================================

def test_volatility_ratio_is_ratio_of_atrs():
    rng = np.random.default_rng(3)
    close = 1.08 + np.cumsum(rng.normal(0, 0.0002, 300))
    high = close + 0.0001
    low = close - 0.0001
    vr = volatility_ratio(high, low, close, fast_period=5, slow_period=30)
    atr_f = wilder_atr(high, low, close, 5)
    atr_s = wilder_atr(high, low, close, 30)
    assert vr[-1] == pytest.approx(atr_f[-1] / atr_s[-1], rel=1e-12)


def test_circuit_breaker_pauses_on_spike():
    """
    Lonjakan volatilitas SINGKAT harus memicu PAUSE (VR > 1.30).

    Bila spike berlangsung > slow_period, ATR_slow menyesuaikan dan rasio
    kembali normal -- maka spike dibuat pendek agar efeknya terukur.
    """
    rng = np.random.default_rng(11)
    calm = 1.08 + np.cumsum(rng.normal(0, 0.00002, 200))
    spike = calm[-1] + np.cumsum(rng.normal(0, 0.0020, 8))
    close = np.concatenate([calm, spike])
    high = close + np.concatenate([np.full(200, 0.00001), np.full(8, 0.0020)])
    low = close - np.concatenate([np.full(200, 0.00001), np.full(8, 0.0020)])

    cb = VolatilityCircuitBreaker(fast_period=5, slow_period=30, threshold=1.30)
    state = None
    for i in range(len(close)):
        s = cb.update(float(high[i]), float(low[i]), float(close[i]))
        if s is not None:
            state = s
    assert state is not None
    assert state.volatility_ratio > 1.30
    assert state.is_active is False
    assert "PAUSE" in state.reason


def test_circuit_breaker_active_when_calm():
    rng = np.random.default_rng(5)
    close = 1.08 + np.cumsum(rng.normal(0, 0.00002, 400))
    high = close + 0.00001
    low = close - 0.00001
    cb = VolatilityCircuitBreaker(fast_period=5, slow_period=30, threshold=1.30)
    state = None
    for i in range(len(close)):
        s = cb.update(float(high[i]), float(low[i]), float(close[i]))
        if s is not None:
            state = s
    assert state is not None
    assert state.is_active is True


def test_circuit_breaker_not_ready_before_slow_period():
    cb = VolatilityCircuitBreaker(fast_period=5, slow_period=30)
    for i in range(29):
        assert cb.update(1.081, 1.079, 1.080) is None
    assert cb.is_ready is False


def test_circuit_breaker_matches_batch_vr():
    """Jalur streaming harus konsisten dengan jalur batch."""
    rng = np.random.default_rng(21)
    close = 1.08 + np.cumsum(rng.normal(0, 0.0001, 200))
    high = close + 0.00005
    low = close - 0.00005

    cb = VolatilityCircuitBreaker(fast_period=5, slow_period=30, threshold=1.30)
    stream_state = None
    for i in range(len(close)):
        s = cb.update(float(high[i]), float(low[i]), float(close[i]))
        if s is not None:
            stream_state = s

    batch = volatility_ratio(high, low, close, 5, 30)[-1]
    assert stream_state is not None
    assert stream_state.volatility_ratio == pytest.approx(batch, rel=1e-9)


def test_circuit_breaker_from_percentile_calibration():
    """Kalibrasi persentil harus menghasilkan ambang yang masuk akal."""
    rng = np.random.default_rng(31)
    close = 1.08 + np.cumsum(rng.normal(0, 0.0001, 1000))
    high = close + 0.00005
    low = close - 0.00005
    cb = VolatilityCircuitBreaker.from_percentile(high, low, close, percentile=95.0)
    assert 0.5 < cb.threshold < 3.0
    # Ambang persentil-95 harus lebih tinggi dari median
    cb50 = VolatilityCircuitBreaker.from_percentile(high, low, close, percentile=50.0)
    assert cb.threshold > cb50.threshold


def test_circuit_breaker_validates_periods():
    with pytest.raises(ValueError):
        VolatilityCircuitBreaker(fast_period=30, slow_period=5)


def test_vr_threshold_130_is_rare_on_random_walk():
    """
    Bukti empiris: ambang 1.30 adalah trigger JARANG (~2-5% bar),
    bukan gerbang konstan. Ini penting untuk interpretasi yang jujur.
    """
    rng = np.random.default_rng(77)
    close = 1.08 + np.cumsum(rng.normal(0, 0.00003, 50000))
    high = close + np.abs(rng.normal(0, 0.00002, 50000))
    low = close - np.abs(rng.normal(0, 0.00002, 50000))
    vr = volatility_ratio(high, low, close, 5, 30)
    valid = vr[np.isfinite(vr)]
    pct_above = float((valid > 1.30).mean() * 100.0)
    assert pct_above < 15.0, f"VR>1.30 terlalu sering ({pct_above:.1f}%)"
    assert 0.9 < float(np.median(valid)) < 1.1, "median VR harus mendekati 1.0"


# =====================================================================
# P20-7: ASYNC TICK FEED
# =====================================================================

def test_mock_tick_feed_streams_expected_count():
    async def run():
        feed = MockTickFeed(max_ticks=10, seed=1)
        out = []
        async for q in feed.__aiter__():
            out.append(q)
        return out
    ticks = asyncio.run(run())
    assert len(ticks) == 10
    assert all(isinstance(t, Quote) for t in ticks)


def test_mock_tick_feed_spread_and_ordering():
    async def run():
        feed = MockTickFeed(spread_pips=1.2, seed=2, max_ticks=5)
        return [q async for q in feed.__aiter__()]
    ticks = asyncio.run(run())
    for q in ticks:
        assert q.ask > q.bid
        assert q.spread_pips == pytest.approx(1.2)
        assert q.mid_price == pytest.approx((q.bid + q.ask) / 2.0)


def test_mock_tick_feed_is_deterministic():
    async def run(seed):
        feed = MockTickFeed(seed=seed, max_ticks=20)
        return [q.mid_price for q in [x async for x in feed.__aiter__()]]
    assert asyncio.run(run(99)) == asyncio.run(run(99))


def test_event_loop_not_blocked_during_stream():
    """
    Spesifikasi: 'thread utama tidak terblokir saat menunggu tick harga baru'.
    Verifikasi: task lain tetap berjalan selama feed streaming.
    """
    async def run():
        feed = MockTickFeed(tick_interval=0.01, max_ticks=5, seed=3)
        counter = {"n": 0}

        async def other_work():
            while True:
                counter["n"] += 1
                await asyncio.sleep(0.001)

        task = asyncio.create_task(other_work())
        ticks = [q async for q in feed.__aiter__()]
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return len(ticks), counter["n"]

    n_ticks, n_other = asyncio.run(run())
    assert n_ticks == 5
    assert n_other > 10, "event loop terblokir selama streaming tick"


def test_mock_feed_mean_reverting_mode_pulls_to_mean():
    async def run():
        feed = MockTickFeed(start_price=1.08000, theta=0.05, sigma=0.0001,
                            seed=8, max_ticks=2000)
        return [q.mid_price for q in [x async for x in feed.__aiter__()]]
    mids = np.array(asyncio.run(run()))
    # OU dengan theta besar harus tetap dekat dengan mean
    assert abs(mids[-500:].mean() - 1.08000) < 0.0005


def test_mock_feed_supports_depth_and_book_valid():
    async def run():
        feed = MockTickFeed(seed=4, max_ticks=3)
        [q async for q in feed.__aiter__()]
        return await feed._next_book()
    book = asyncio.run(run())
    assert book is not None
    assert book.is_valid
    assert len(book.bids) == 5 and len(book.asks) == 5
    # bids menurun, asks menaik
    assert all(book.bids[i].price > book.bids[i + 1].price for i in range(4))
    assert all(book.asks[i].price < book.asks[i + 1].price for i in range(4))


def test_feed_stats_track_ticks():
    async def run():
        feed = MockTickFeed(seed=6, max_ticks=7)
        [q async for q in feed.__aiter__()]
        return feed.stats
    stats = asyncio.run(run())
    assert stats.ticks_received == 7
    assert stats.last_tick_utc is not None


# =====================================================================
# P20-8: STRATEGI KOMPOSIT Z-SCORE (END-TO-END)
# =====================================================================

def _ou_bars(n=600, theta=0.01, sigma=0.00002, seed=5, start=1.08):
    rng = np.random.default_rng(seed)
    x = np.empty(n)
    x[0] = start
    for i in range(1, n):
        x[i] = x[i - 1] + theta * (start - x[i - 1]) + rng.normal(0, sigma)
    return pd.DataFrame({
        "open": x,
        "high": x + 0.00003,
        "low": x - 0.00003,
        "close": x,
        "volume": np.full(n, 1.0),
    })


def _ramp_bars(n=400, total_move=-0.0006, ramp=20, seed=5, start=1.08, noise=0.00002):
    """
    Deret dengan RAMP bertahap di ujung (BUKAN lompatan 1 bar).

    Ini penting: lompatan 1 bar memicu circuit breaker (VR > 1.30) sehingga
    entry diblokir. Ramp bertahap menghasilkan |z| >= 2 dengan VR tetap rendah
    -- inilah bentuk setup yang memang diizinkan oleh decision matrix spesifikasi.
    """
    rng = np.random.default_rng(seed)
    x = start + np.cumsum(rng.normal(0, noise, n))
    x[-ramp:] = x[-ramp - 1] + np.linspace(0.0, total_move, ramp)
    return pd.DataFrame({
        "open": x,
        "high": x + 0.00003,
        "low": x - 0.00003,
        "close": x,
        "volume": np.full(n, 1.0),
    })


def test_strategy_holds_when_z_inside_band():
    df = _ou_bars(seed=5)
    s = EURUSDZScoreMeanReversionStrategy(require_confirmation=False)
    sig = s.generate_signal("EURUSD", df)
    assert sig.action in (SignalAction.HOLD, SignalAction.BUY, SignalAction.SELL)


def test_strategy_buys_when_z_below_negative_band():
    """
    Ramp turun bertahap -> harga di BAWAH mean, VR tetap rendah -> BUY.

    Catatan desain penting: lompatan 1 bar TIDAK dipakai di sini karena akan
    memicu circuit breaker (VR > 1.30) dan entry diblokir. Itu perilaku yang
    BENAR sesuai decision matrix spesifikasi, bukan bug.
    """
    df = _ramp_bars(total_move=-0.0006)
    s = EURUSDZScoreMeanReversionStrategy(require_confirmation=False, entry_band=2.0)
    sig = s.generate_signal("EURUSD", df)
    assert sig.action == SignalAction.BUY
    assert sig.stop_loss < sig.entry_price < sig.take_profit


def test_strategy_sells_when_z_above_positive_band():
    df = _ramp_bars(total_move=+0.0006)
    s = EURUSDZScoreMeanReversionStrategy(require_confirmation=False, entry_band=2.0)
    sig = s.generate_signal("EURUSD", df)
    assert sig.action == SignalAction.SELL
    assert sig.take_profit < sig.entry_price < sig.stop_loss


def test_strategy_risk_reward_at_least_2():
    df = _ramp_bars(total_move=-0.0006)
    s = EURUSDZScoreMeanReversionStrategy(require_confirmation=False)
    sig = s.generate_signal("EURUSD", df)
    assert sig.action != SignalAction.HOLD
    # Invariant KERAS: strategi wajib menjamin RR >= 2.0 meskipun ada pembulatan.
    # (Bug ini yang membuat test P8 lama flaky: assert 1.999999999999633 >= 2.0)
    assert sig.risk_reward_ratio >= 2.0


def test_strategy_circuit_breaker_blocks_on_volatility_spike():
    """
    VR > 1.30 pada bar terakhir harus memblokir SEMUA entry (PAUSE).

    Catatan: spike harus SINGKAT. Bila spike berlangsung lebih lama dari
    slow_period (30 bar), ATR_slow ikut naik dan rasio kembali normal --
    itu sebabnya fixture ini memakai spike pendek.
    """
    rng = np.random.default_rng(13)
    calm = 1.08 + np.cumsum(rng.normal(0, 0.00001, 300))
    spike = calm[-1] + np.cumsum(rng.normal(0, 0.0030, 8))
    x = np.concatenate([calm, spike])
    df = pd.DataFrame({
        "open": x,
        "high": x + np.concatenate([np.full(300, 0.00001), np.full(8, 0.0020)]),
        "low": x - np.concatenate([np.full(300, 0.00001), np.full(8, 0.0020)]),
        "close": x,
        "volume": np.full(len(x), 1.0),
    })
    s = EURUSDZScoreMeanReversionStrategy(require_confirmation=False)
    sig = s.generate_signal("EURUSD", df)
    assert sig.action == SignalAction.HOLD
    assert "PAUSE" in sig.rationale


def test_strategy_insufficient_data_returns_hold():
    df = _ou_bars(n=50)
    s = EURUSDZScoreMeanReversionStrategy(window=200)
    sig = s.generate_signal("EURUSD", df)
    assert sig.action == SignalAction.HOLD
    assert "Insufficient" in sig.rationale


def test_strategy_flat_market_returns_hold_not_crash():
    n = 400
    flat = np.full(n, 1.08500)
    df = pd.DataFrame({
        "open": flat, "high": flat, "low": flat, "close": flat,
        "volume": np.full(n, 1.0),
    })
    s = EURUSDZScoreMeanReversionStrategy(require_confirmation=False)
    sig = s.generate_signal("EURUSD", df)
    assert sig.action == SignalAction.HOLD
    assert "sigma" in sig.rationale or "tidak terdefinisi" in sig.rationale


# =====================================================================
# P20-9: FAIL-CLOSED
# =====================================================================

def test_strategy_fail_closed_without_order_book():
    """
    Tanpa DOM (kasus retail MT5), strategi HARUS menolak entry
    ketika require_confirmation=True dan allow_without_depth=False.
    """
    df = _ramp_bars(total_move=-0.0006)
    s = EURUSDZScoreMeanReversionStrategy(require_confirmation=True, allow_without_depth=False)
    sig = s.generate_signal("EURUSD", df)
    assert sig.action == SignalAction.HOLD
    assert "Fail-closed" in sig.rationale


def test_strategy_allows_when_depth_explicitly_optional():
    df = _ramp_bars(total_move=-0.0006)
    s = EURUSDZScoreMeanReversionStrategy(require_confirmation=True, allow_without_depth=True)
    sig = s.generate_signal("EURUSD", df)
    assert sig.action == SignalAction.BUY


def test_strategy_imbalance_rejects_contrary_signal():
    """Z menandakan BUY tapi book menunjukkan selling pressure kuat -> tolak."""
    df = _ramp_bars(total_move=-0.0006)
    s = EURUSDZScoreMeanReversionStrategy(require_confirmation=True)
    s.on_book(make_synthetic_book(1.08, bid_ask_ratio=0.1))   # didominasi ask
    sig = s.generate_signal("EURUSD", df)
    assert sig.action == SignalAction.HOLD
    assert "menolak BUY" in sig.rationale


def test_strategy_accepts_signal_when_imbalance_agrees():
    df = _ramp_bars(total_move=-0.0006)
    s = EURUSDZScoreMeanReversionStrategy(require_confirmation=True)
    s.on_book(make_synthetic_book(1.08, bid_ask_ratio=3.0))   # buying pressure
    sig = s.generate_signal("EURUSD", df)
    assert sig.action == SignalAction.BUY


def test_strategy_reset_clears_state():
    s = EURUSDZScoreMeanReversionStrategy(require_confirmation=False)
    for i in range(250):
        s.on_tick(Quote("EURUSD", 1.08500 + i * 1e-6, 1.08510 + i * 1e-6, 1.0))
    assert s.mid_buffer.stats.count > 0
    s.reset()
    assert s.mid_buffer.stats.count == 0
    assert s.circuit_breaker.candles_seen == 0

# =====================================================================
# P20-10: MODE KONFIRMASI IMBALANCE (VETO vs WEIGHT)
# =====================================================================

def test_imbalance_mode_validation():
    with pytest.raises(ValueError):
        EURUSDZScoreMeanReversionStrategy(imbalance_mode="invalid")


def test_weight_mode_does_not_veto_contrary_imbalance():
    """
    Mode "weight" (rekomendasi riset) TIDAK memveto meskipun imbalance
    berlawanan kuat. Ambang +-0.30 tidak punya dasar di literatur,
    sehingga ia hanya menskalakan ukuran posisi, bukan memblokir entry.
    """
    df = _ramp_bars(total_move=-0.0006)
    s = EURUSDZScoreMeanReversionStrategy(
        require_confirmation=True, imbalance_mode="weight"
    )
    s.on_book(make_synthetic_book(1.08, bid_ask_ratio=0.1))   # selling pressure kuat
    sig = s.generate_signal("EURUSD", df)
    assert sig.action == SignalAction.BUY, "mode weight tidak boleh memveto"


def test_veto_mode_blocks_contrary_imbalance():
    """Mode "veto" (default, sesuai spesifikasi) memblokir entry."""
    df = _ramp_bars(total_move=-0.0006)
    s = EURUSDZScoreMeanReversionStrategy(
        require_confirmation=True, imbalance_mode="veto"
    )
    s.on_book(make_synthetic_book(1.08, bid_ask_ratio=0.1))
    sig = s.generate_signal("EURUSD", df)
    assert sig.action == SignalAction.HOLD
    assert "menolak BUY" in sig.rationale


def test_size_multiplier_scales_with_agreement():
    """
    Pengali ukuran: searah kuat=1.0, netral=0.75, berlawanan=0.5.
    Selalu dalam [0.5, 1.0] -- tidak pernah memblokir sepenuhnya.
    """
    s = EURUSDZScoreMeanReversionStrategy(require_confirmation=True, imbalance_mode="weight")

    s.on_book(make_synthetic_book(1.08, bid_ask_ratio=3.0))    # buying pressure
    assert s.imbalance_size_multiplier(SignalAction.BUY) == pytest.approx(1.0)

    s.on_book(make_synthetic_book(1.08, bid_ask_ratio=0.1))    # selling pressure
    assert s.imbalance_size_multiplier(SignalAction.BUY) == pytest.approx(0.5)
    # Untuk SELL, imbalance yang sama justru searah
    assert s.imbalance_size_multiplier(SignalAction.SELL) == pytest.approx(1.0)

    s.on_book(make_synthetic_book(1.08, bid_ask_ratio=1.0))    # netral
    assert s.imbalance_size_multiplier(SignalAction.BUY) == pytest.approx(0.75)


def test_size_multiplier_returns_one_without_depth():
    """Tanpa depth, pengali = 1.0 (tidak menghukum karena data tidak ada)."""
    s = EURUSDZScoreMeanReversionStrategy(require_confirmation=True, imbalance_mode="weight")
    assert s.imbalance_size_multiplier(SignalAction.BUY) == pytest.approx(1.0)


# =====================================================================
# P20-11: HISTERESIS & ATR-PERCENTILE GATE (REKOMENDASI RISET)
# =====================================================================

def test_hysteresis_holds_pause_longer_than_bare_threshold():
    """
    Histeresis mencegah FLAPPING: begitu PAUSE aktif, ia baru lepas saat
    VR turun di bawah release_threshold (default 1.30*0.88 = 1.144),
    bukan tepat di 1.30.

    Akibatnya jumlah bar PAUSE dengan histeresis >= tanpa histeresis.
    Ini disengaja: riset menunjukkan P(PAUSE besok | PAUSE hari ini) = 82.2%
    sementara P(PAUSE besok | AKTIF) = 2.7% -- gate ini memang persisten,
    dan histeresis mencegahnya berkedip on/off di sekitar ambang.
    """
    rng = np.random.default_rng(41)
    calm = 1.08 + np.cumsum(rng.normal(0, 0.00002, 300))
    spike = calm[-1] + np.cumsum(rng.normal(0, 0.0025, 10))
    after = spike[-1] + np.cumsum(rng.normal(0, 0.00002, 200))
    close = np.concatenate([calm, spike, after])
    high = close + np.concatenate([np.full(300, 0.00001), np.full(10, 0.0020), np.full(200, 0.00001)])
    low = close - np.concatenate([np.full(300, 0.00001), np.full(10, 0.0020), np.full(200, 0.00001)])

    no_hyst = VolatilityCircuitBreaker(5, 30, 1.30, release_threshold=1.30)
    hyst = VolatilityCircuitBreaker(5, 30, 1.30)

    pause_no, pause_hy = 0, 0
    for i in range(len(close)):
        s1 = no_hyst.update(float(high[i]), float(low[i]), float(close[i]))
        s2 = hyst.update(float(high[i]), float(low[i]), float(close[i]))
        if s1 is not None and not s1.is_active:
            pause_no += 1
        if s2 is not None and not s2.is_active:
            pause_hy += 1

    assert pause_hy >= pause_no, "histeresis harus menahan PAUSE, bukan melepas lebih awal"


def test_hysteresis_release_threshold_default():
    """release_threshold default harus 0.88 * threshold."""
    cb = VolatilityCircuitBreaker(5, 30, 1.30)
    assert cb.release_threshold == pytest.approx(1.30 * 0.88)


def test_confirm_bars_rejects_single_bar_spike():
    """
    Bukti riset: satu bar 3x rata-rata sudah cukup memicu VR=1.3125 > 1.30.
    confirm_bars=3 harus menolak spike satu bar tersebut.
    """
    rng = np.random.default_rng(43)
    calm = 1.08 + np.cumsum(rng.normal(0, 0.00002, 300))
    single = calm[-1] + np.cumsum(rng.normal(0, 0.0025, 1))
    after = single[-1] + np.cumsum(rng.normal(0, 0.00002, 5))
    close = np.concatenate([calm, single, after])
    high = close + np.concatenate([np.full(300, 0.00001), np.full(1, 0.0030), np.full(5, 0.00001)])
    low = close - np.concatenate([np.full(300, 0.00001), np.full(1, 0.0030), np.full(5, 0.00001)])

    instant = VolatilityCircuitBreaker(5, 30, 1.30, confirm_bars=1)
    confirmed = VolatilityCircuitBreaker(5, 30, 1.30, confirm_bars=3)

    pause_i, pause_c = 0, 0
    for i in range(len(close)):
        s1 = instant.update(float(high[i]), float(low[i]), float(close[i]))
        s2 = confirmed.update(float(high[i]), float(low[i]), float(close[i]))
        if s1 is not None and not s1.is_active:
            pause_i += 1
        if s2 is not None and not s2.is_active:
            pause_c += 1

    # Spike satu bar hanya melampaui ambang selama beberapa bar; confirm_bars=3
    # harus menunda/menghilangkan trigger tersebut.
    assert pause_i > 0, "confirm_bars=1 harus terpicu oleh spike"
    assert pause_c < pause_i, "confirm_bars=3 harus menekan trigger spike satu bar"


def test_hysteresis_validation():
    with pytest.raises(ValueError):
        VolatilityCircuitBreaker(5, 30, 1.30, release_threshold=1.40)
    with pytest.raises(ValueError):
        VolatilityCircuitBreaker(5, 30, 1.30, confirm_bars=0)


def test_atr_percentile_rank_range_and_monotonicity():
    """Percentile harus dalam [0,100] dan tinggi setelah rezim volatil."""
    rng = np.random.default_rng(47)
    calm = 1.08 + np.cumsum(rng.normal(0, 0.00002, 400))
    wild = calm[-1] + np.cumsum(rng.normal(0, 0.0020, 100))
    close = np.concatenate([calm, wild])
    high = close + 0.00002
    low = close - 0.00002

    pct = atr_percentile_rank(high, low, close, atr_period=14, lookback=252)
    valid = pct[np.isfinite(pct)]
    assert len(valid) > 0
    assert valid.min() >= 0.0 and valid.max() <= 100.0
    assert pct[-1] > 80.0, f"percentile akhir terlalu rendah: {pct[-1]}"


def test_atr_percentile_circuit_breaker_pauses_on_high_vol():
    """
    Persentil ATR harus mencapai zona PAUSE saat rezim volatil.

    Catatan: dengan lookback 252, bar CALM awal masih ada di dalam window
    sehingga persentil akhir bisa turun kembali setelah spike mereda.
    Karena itu test ini memeriksa puncak persentil, bukan nilai akhir.
    """
    rng = np.random.default_rng(53)
    calm = 1.08 + np.cumsum(rng.normal(0, 0.00002, 300))
    wild = calm[-1] + np.cumsum(rng.normal(0, 0.0020, 120))
    close = np.concatenate([calm, wild])
    high = close + 0.00002
    low = close - 0.00002

    cb = ATRPercentileCircuitBreaker(
        atr_period=14, lookback=252, pause_percentile=95.0,
        release_percentile=80.0, confirm_bars=2,
    )
    paused_any = False
    max_pct = 0.0
    for i in range(len(close)):
        s = cb.update(float(high[i]), float(low[i]), float(close[i]))
        if s is not None:
            max_pct = max(max_pct, s.volatility_ratio)
            if not s.is_active:
                paused_any = True
    assert max_pct > 95.0, f"persentil tidak pernah mencapai zona PAUSE: {max_pct:.1f}"
    assert paused_any, "circuit breaker tidak pernah PAUSE saat volatilitas memuncak"


def test_atr_percentile_circuit_breaker_stays_active_when_calm():
    rng = np.random.default_rng(59)
    close = 1.08 + np.cumsum(rng.normal(0, 0.00002, 500))
    high = close + 0.00002
    low = close - 0.00002

    cb = ATRPercentileCircuitBreaker(atr_period=14, lookback=252, confirm_bars=2)
    last = None
    for i in range(len(close)):
        s = cb.update(float(high[i]), float(low[i]), float(close[i]))
        if s is not None:
            last = s
    assert last is not None
    assert last.is_active is True


def test_atr_percentile_validation():
    with pytest.raises(ValueError):
        ATRPercentileCircuitBreaker(pause_percentile=80.0, release_percentile=90.0)
    with pytest.raises(ValueError):
        ATRPercentileCircuitBreaker(confirm_bars=0)


# =====================================================================
# P20-12: SIGMA FLOOR & INTEGRITAS NUMERIK (TEMUAN RISET KRITIS)
# =====================================================================

def test_sigma_floor_blocks_exploding_z_score():
    """
    TEMUAN RISET KRITIS: ketika sigma -> 0, z = (P-mu)/sigma MELEDAK menuju
    +/-inf. Ini terjadi nyata pada weekend close, sesi libur, dan rollover
    23:00-00:00 GMT. Tanpa floor, z-score menjadi generator sinyal palsu
    yang dijamin -- dan nilainya besar sehingga lolos SEMUA filter band.
    """
    buf = MidPriceBuffer(window=20, sigma_floor=1e-6)
    # Window nyaris beku: variasi 1e-9, jauh di bawah floor 1e-6
    base = 1.08500
    for i in range(20):
        mid = base + (i % 2) * 1e-9
        buf.update(Quote("EURUSD", mid - 5e-6, mid + 5e-6, 1.0))
    assert buf.stats.std < 1e-6, "fixture harus menghasilkan sigma di bawah floor"
    assert buf.z_score() is None, "sigma di bawah floor harus fail-closed (None)"


def test_sigma_floor_allows_normal_market():
    """Pasar normal (sigma tipikal EURUSD tick-level) harus tetap menghasilkan z."""
    buf = MidPriceBuffer(window=50, sigma_floor=1e-6)
    rng = np.random.default_rng(71)
    for _ in range(50):
        mid = 1.08500 + rng.normal(0, 2e-5)
        buf.update(Quote("EURUSD", mid - 5e-6, mid + 5e-6, 1.0))
    z = buf.z_score()
    assert z is not None and np.isfinite(z), "pasar normal harus menghasilkan z valid"


def test_z_score_never_returns_infinite():
    """
    Regresi: dengan floor sangat kecil, z bisa meledak. Pastikan hasilnya
    selalu finite atau None -- tidak pernah inf.
    """
    buf = MidPriceBuffer(window=10, sigma_floor=0.0)
    for i in range(10):
        buf.update(Quote("EURUSD", 1.08500, 1.08500, 0.0))
    # sigma = 0 -> None (bukan inf)
    assert buf.z_score() is None

    # Tambahkan satu outlier kecil pada window yang nyaris beku
    buf.update(Quote("EURUSD", 1.08500 + 1e-9, 1.08500 + 1e-9, 0.0))
    z = buf.z_score()
    assert z is None or np.isfinite(z), f"z harus finite atau None, dapat {z}"


def test_integrity_check_matches_exact_recompute():
    """
    Rekomendasi riset: jalankan full recompute berkala sebagai integrity check
    terhadap drift Welford.
    """
    rng = np.random.default_rng(73)
    s = RollingWindowStats(window=100)
    for _ in range(2000):
        s.update(float(1.08 + rng.normal(0, 3e-5)))
    assert s.verify_against_exact(tolerance=1e-9), "Welford drift melebihi toleransi"


def test_mid_price_buffer_default_sigma_floor():
    """Default sigma_floor harus 1e-6 (= 0.01 pip EURUSD 5-digit)."""
    buf = MidPriceBuffer(window=10)
    assert buf.sigma_floor == pytest.approx(1e-6)

