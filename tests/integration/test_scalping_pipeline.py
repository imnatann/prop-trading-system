"""
Integration Tests for the Async Micro-Scalping Pipeline (Alpha v2).
Menguji SERVICE end-to-end, bukan hanya unit komponen:

    TickFeed (async) -> MidPriceBuffer -> Z-Score -> Circuit Breaker -> Signal

Fokus: membuktikan pipeline BENAR-BENAR terpasang dan event loop tidak diblokir,
yang merupakan kelemahan utama yang ditemukan pada audit sebelumnya
(services/trading_engine.py tidak pernah menyentuh strategi maupun data market).
"""

import asyncio

import numpy as np
import pytest

from src.data.tick_feed import MockTickFeed
from src.strategy.base import SignalAction
from src.strategy.zscore_scalper import EURUSDZScoreMeanReversionStrategy
from services.scalping_service import MicroScalpingService


def _run_service(max_ticks=2000, window=50, require_confirmation=False,
                 virtual_clock=1.0, theta=0.02, sigma=0.00005, seed=3,
                 bar_seconds=60, collect_book=True):
    async def run():
        strat = EURUSDZScoreMeanReversionStrategy(
            window=window, require_confirmation=require_confirmation
        )
        feed = MockTickFeed(
            theta=theta, sigma=sigma, seed=seed, max_ticks=max_ticks,
            virtual_clock_seconds=virtual_clock,
        )
        svc = MicroScalpingService(
            strategy=strat, feed=feed, bar_seconds=bar_seconds,
            collect_book=collect_book,
        )
        stats = await svc.run()
        return svc, stats
    return asyncio.run(run())


def test_service_processes_ticks_end_to_end():
    svc, stats = _run_service(max_ticks=1500)
    assert stats.ticks_processed == 1500
    assert stats.decisions_evaluated == 1500


def test_service_aggregates_bars_and_updates_circuit_breaker():
    """
    Bar M1 HARUS tertutup sehingga circuit breaker benar-benar ter-update.
    Regresi: bug sebelumnya membuat bars_closed=0 sehingga VR tidak pernah dihitung.
    """
    svc, stats = _run_service(max_ticks=4000, virtual_clock=1.0)
    assert stats.bars_closed > 50, f"bar tidak tertutup: {stats.bars_closed}"
    assert svc._atr is not None and svc._atr > 0


def test_service_collects_order_books():
    """
    Order book HARUS ter-poll. Regresi: bug starvation membuat books=0.
    """
    svc, stats = _run_service(max_ticks=2000)
    assert stats.books_processed > 100, f"book tidak ter-poll: {stats.books_processed}"


def test_service_generates_signals():
    svc, stats = _run_service(max_ticks=4000)
    assert stats.signals_generated > 0
    assert stats.last_signal is not None
    assert stats.last_signal.action in (SignalAction.BUY, SignalAction.SELL)


def test_generated_signals_have_valid_sl_tp():
    """Setiap sinyal harus punya SL/TP yang benar dan RR >= 2.0."""
    svc, stats = _run_service(max_ticks=4000)
    sig = stats.last_signal
    assert sig is not None
    if sig.action == SignalAction.BUY:
        assert sig.stop_loss < sig.entry_price < sig.take_profit
    else:
        assert sig.take_profit < sig.entry_price < sig.stop_loss
    assert sig.risk_reward_ratio >= 2.0


def test_event_loop_not_blocked_by_pipeline():
    """
    Persyaratan spesifikasi: 'thread utama tidak terblokir saat menunggu tick'.
    Task independen harus tetap berjalan selama pipeline aktif.
    """
    async def run():
        strat = EURUSDZScoreMeanReversionStrategy(window=50, require_confirmation=False)
        feed = MockTickFeed(theta=0.02, sigma=0.00005, seed=3, max_ticks=3000)
        svc = MicroScalpingService(strategy=strat, feed=feed, bar_seconds=60)
        counter = {"n": 0}

        async def other_work():
            while True:
                counter["n"] += 1
                await asyncio.sleep(0)

        task = asyncio.create_task(other_work())
        stats = await svc.run()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return stats, counter["n"]

    stats, n_other = asyncio.run(run())
    assert stats.ticks_processed == 3000
    assert n_other > 100, f"event loop terblokir (hanya {n_other} iterasi task lain)"


def test_service_blocks_signals_when_depth_missing():
    """
    Fail-closed: dengan require_confirmation=True dan collect_book=False,
    TIDAK boleh ada sinyal yang lolos.
    """
    svc, stats = _run_service(max_ticks=4000, require_confirmation=True, collect_book=False)
    assert stats.signals_generated == 0
    assert stats.signals_blocked_by_missing_depth > 0


def test_service_circuit_breaker_blocks_during_volatility_spike():
    """
    Peralihan rezim volatilitas harus memicu blokir circuit breaker.

    CATATAN PENTING: VR = ATR_fast/ATR_slow mengukur PERUBAHAN volatilitas,
    bukan LEVEL volatilitas. Volatilitas tinggi yang KONSTAN tetap
    menghasilkan VR ~ 1.0. Karena itu test ini memakai peralihan rezim
    (calm -> stress), bukan sekadar sigma besar.
    """
    async def run():
        strat = EURUSDZScoreMeanReversionStrategy(window=50, require_confirmation=False)
        feed = MockTickFeed(
            theta=0.0, sigma=0.00002, seed=11, max_ticks=3000,
            virtual_clock_seconds=1.0,
            stress_at_tick=1500, stress_sigma_multiplier=15.0,
        )
        svc = MicroScalpingService(strategy=strat, feed=feed, bar_seconds=60)
        return await svc.run()

    stats = asyncio.run(run())
    assert stats.signals_blocked_by_circuit_breaker > 0, "circuit breaker tidak pernah memblokir saat rezim volatilitas berubah"


def test_constant_high_volatility_does_not_trip_circuit_breaker():
    """
    Dokumentasi perilaku (dan batasan) VR: volatilitas tinggi yang KONSTAN
    tidak memicu PAUSE. Ini menjelaskan mengapa ambang 1.30 jarang menyala.
    """
    async def run():
        strat = EURUSDZScoreMeanReversionStrategy(window=50, require_confirmation=False)
        feed = MockTickFeed(theta=0.0, sigma=0.0020, seed=11, max_ticks=3000,
                            virtual_clock_seconds=1.0)
        svc = MicroScalpingService(strategy=strat, feed=feed, bar_seconds=60)
        return await svc.run()

    stats = asyncio.run(run())
    assert stats.signals_blocked_by_circuit_breaker == 0


def test_service_is_deterministic_with_seed():
    _, a = _run_service(max_ticks=1500, seed=77)
    _, b = _run_service(max_ticks=1500, seed=77)
    assert a.ticks_processed == b.ticks_processed
    assert a.signals_generated == b.signals_generated
    assert a.bars_closed == b.bars_closed


def test_service_stats_summary_is_informative():
    svc, stats = _run_service(max_ticks=1000)
    s = stats.summary()
    assert "ticks=" in s and "signals=" in s and "blocked:" in s


def test_on_signal_callback_is_invoked():
    """Callback harus dipanggil untuk setiap sinyal (integrasi ke eksekusi)."""
    collected = []

    async def run():
        strat = EURUSDZScoreMeanReversionStrategy(window=50, require_confirmation=False)
        feed = MockTickFeed(theta=0.02, sigma=0.00005, seed=3, max_ticks=4000)
        svc = MicroScalpingService(
            strategy=strat, feed=feed, bar_seconds=60,
            on_signal=lambda s: collected.append(s),
        )
        return await svc.run()

    stats = asyncio.run(run())
    assert len(collected) == stats.signals_generated
    assert len(collected) > 0
