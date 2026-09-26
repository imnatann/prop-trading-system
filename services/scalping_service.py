"""
Async Micro-Scalping Ingestion Service (Alpha v2).
Merangkai arsitektur yang diminta menjadi satu pipeline yang benar-benar jalan:

    TickFeed (async) --> MidPriceBuffer (Welford) --> Z-Score
                              |
                              +--> VolatilityCircuitBreaker (bar M1)
                              +--> OrderBookImbalance (konfirmasi)
                              |
                              v
                        ScalpDecision --> TradeSignal

Service ini adalah jawaban atas temuan audit: sebelumnya services/trading_engine.py
TIDAK pernah menyentuh strategi maupun data market. Service ini benar-benar
meng-orkestrasi keduanya, dan menulis heartbeat yang sama agar watchdog tetap
dapat mengawasi.

PENTING -- status kejujuran:
Service ini diuji secara end-to-end dengan MockTickFeed. Untuk LIVE, feed MT5
memerlukan Windows VPS dan DOM EURUSD yang umumnya TIDAK tersedia di broker
retail, sehingga konfirmasi order book akan fail-closed.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional

import numpy as np
from loguru import logger

from src.data.market import Quote
from src.data.tick_feed import MockTickFeed, TickFeed
from src.safety.heartbeat import HeartbeatManager
from src.strategy.base import SignalAction, TradeSignal
from src.strategy.zscore_scalper import EURUSDZScoreMeanReversionStrategy, ScalpDecision


@dataclass
class ScalpingServiceStats:
    """Metrik runtime service."""
    ticks_processed: int = 0
    books_processed: int = 0
    bars_closed: int = 0
    decisions_evaluated: int = 0
    signals_generated: int = 0
    signals_blocked_by_circuit_breaker: int = 0
    signals_blocked_by_missing_depth: int = 0
    signals_blocked_by_imbalance: int = 0
    started_utc: Optional[datetime] = None
    last_signal: Optional[TradeSignal] = None
    recent_decisions: List[ScalpDecision] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"ticks={self.ticks_processed} books={self.books_processed} "
            f"bars={self.bars_closed} decisions={self.decisions_evaluated} "
            f"signals={self.signals_generated} "
            f"(blocked: cb={self.signals_blocked_by_circuit_breaker}, "
            f"depth={self.signals_blocked_by_missing_depth}, "
            f"imb={self.signals_blocked_by_imbalance})"
        )


class MicroScalpingService:
    """
    Service ingestion + evaluasi sinyal micro-scalping.

    Alur per tick:
      1. Perbarui MidPriceBuffer (mid price) -> Welford rolling stats
      2. Perbarui OrderFlowImbalanceTracker bila depth tersedia
      3. Akumulasi bar M1; saat bar tutup -> update circuit breaker
      4. Evaluasi Z-Score + konfirmasi + circuit breaker -> ScalpDecision
    """

    def __init__(
        self,
        strategy: Optional[EURUSDZScoreMeanReversionStrategy] = None,
        feed: Optional[TickFeed] = None,
        heartbeat: Optional[HeartbeatManager] = None,
        symbol: str = "EURUSD",
        bar_seconds: int = 60,
        on_signal: Optional[Callable[[TradeSignal], None]] = None,
        collect_book: bool = True,
    ):
        self.symbol = symbol.upper()
        self.strategy = strategy or EURUSDZScoreMeanReversionStrategy()
        self.feed = feed or MockTickFeed(symbol=self.symbol)
        self.heartbeat = heartbeat
        self.bar_seconds = bar_seconds
        self.on_signal = on_signal
        self.collect_book = collect_book

        self.stats = ScalpingServiceStats()
        self._bar_high: Optional[float] = None
        self._bar_low: Optional[float] = None
        self._bar_open: Optional[float] = None
        self._bar_start: Optional[datetime] = None
        self._atr: Optional[float] = None
        self._running = False

    # ------------------------------------------------------------------
    # Bar aggregation (M1) untuk circuit breaker
    # ------------------------------------------------------------------
    def _update_bar(self, quote: Quote) -> None:
        mid = quote.mid_price

        # Inisialisasi bar pertama
        if self._bar_start is None:
            self._start_bar(quote.timestamp_utc, mid)
            return

        # Tutup bar berulang kali bila satu tick melompati beberapa periode bar.
        # (Pada feed berkecepatan tinggi, satu tick bisa melewati >1 bar.)
        while (quote.timestamp_utc - self._bar_start).total_seconds() >= self.bar_seconds:
            self._close_bar(self._bar_start + timedelta(seconds=self.bar_seconds))
            # Bar baru dimulai pada batas periode; seed dengan harga tick saat ini
            # agar _bar_open tidak pernah None (bug sebelumnya: bar berikutnya
            # tidak pernah bisa ditutup karena _bar_open tetap None).
            self._start_bar(self._bar_start, mid)

        self._bar_high = max(self._bar_high if self._bar_high is not None else mid, mid)
        self._bar_low = min(self._bar_low if self._bar_low is not None else mid, mid)

    def _start_bar(self, start: datetime, mid: float) -> None:
        self._bar_start = start
        self._bar_open = mid
        self._bar_high = mid
        self._bar_low = mid

    def _close_bar(self, bar_end: datetime) -> None:
        """
        Tutup bar saat ini dan serahkan ke circuit breaker.

        PENTING: state bar (open/high/low) di-reset SETELAH dikirim, dan bar
        berikutnya dimulai dari bar_end (batas periode), bukan dari waktu tick.
        Sebelumnya bug: bar_start di-reset ke waktu tick sehingga bar tidak
        pernah tertutup berulang -> circuit breaker tidak pernah ter-update.
        """
        if self._bar_high is None or self._bar_low is None or self._bar_open is None:
            self._bar_start = bar_end
            return

        state = self.strategy.circuit_breaker.update(self._bar_high, self._bar_low, self._bar_open)
        self.stats.bars_closed += 1
        if state is not None:
            # ATR_slow dipakai untuk sizing SL/TP (skala volatilitas jangka panjang)
            self._atr = state.atr_slow if state.atr_slow > 0 else self._atr

        # Mulai bar baru pada batas periode berikutnya
        self._bar_start = bar_end
        self._bar_open = self._bar_high = self._bar_low = None

    # ------------------------------------------------------------------
    # Callback tick
    # ------------------------------------------------------------------
    def process_tick(self, quote: Quote) -> Optional[ScalpDecision]:
        """Proses satu tick secara sinkron (dipanggil dari event loop)."""
        self.stats.ticks_processed += 1
        self.strategy.on_tick(quote)
        self._update_bar(quote)

        if self.heartbeat is not None:
            self.heartbeat.write_heartbeat("scalping_service", {
                "status": "RUNNING",
                "ticks": self.stats.ticks_processed,
            })

        decision = self.strategy.evaluate(atr=self._atr or 0.0)
        self.stats.decisions_evaluated += 1
        self.stats.recent_decisions.append(decision)
        if len(self.stats.recent_decisions) > 50:
            self.stats.recent_decisions.pop(0)

        # Klasifikasi alasan blokir (observability)
        if decision.action == SignalAction.HOLD:
            reason = decision.reason.lower()
            if "pause" in reason:
                self.stats.signals_blocked_by_circuit_breaker += 1
            elif "fail-closed" in reason or "order book" in reason:
                self.stats.signals_blocked_by_missing_depth += 1
            elif "menolak" in reason:
                self.stats.signals_blocked_by_imbalance += 1
        return decision

    async def _collect_book(self) -> None:
        if not self.collect_book:
            return
        book = await self.feed._next_book()
        if book is not None:
            self.stats.books_processed += 1
            self.strategy.on_book(book)

    async def _book_worker(self) -> None:
        """
        Poll order book berdasarkan hitungan tick.

        Memberi kesempatan (await asyncio.sleep(0)) agar tidak memonopoli event
        loop, tetapi TIDAK bergantung pada waktu dinding sehingga tetap bekerja
        pada feed berkecepatan tinggi.
        """
        last_seen = -1
        while self._running:
            if self.stats.ticks_processed != last_seen:
                last_seen = self.stats.ticks_processed
                try:
                    await self._collect_book()
                except Exception as exc:
                    logger.debug(f"book_worker: {exc}")
            await asyncio.sleep(0)

    async def run(self, max_ticks: Optional[int] = None) -> ScalpingServiceStats:
        """
        Jalankan pipeline async sampai feed berhenti / max_ticks tercapai.

        Event loop TIDAK diblokir: feed melakukan await, dan pekerjaan lain
        (heartbeat, book polling) dijadwalkan sebagai task terpisah.
        """
        self._running = True
        self.stats.started_utc = datetime.now(timezone.utc)
        logger.info(f"MicroScalpingService start: {self.symbol} window={self.strategy.window}")

        # Book polling dijalankan sebagai task terpisah agar event loop tetap
        # responsif. Karena feed mock berjalan sangat cepat (tanpa sleep nyata),
        # worker berbasis sleep() bisa kelaparan (starvation). Solusinya: poll
        # berdasarkan hitungan tick, bukan waktu dinding.
        book_task = None
        if self.collect_book:
            book_task = asyncio.create_task(self._book_worker())

        try:
            async for quote in self.feed.__aiter__():
                decision = self.process_tick(quote)
                if decision is not None and decision.action != SignalAction.HOLD:
                    signal = self._decision_to_signal(decision, quote)
                    if signal is not None:
                        self.stats.signals_generated += 1
                        self.stats.last_signal = signal
                        logger.info(f"SIGNAL {signal.action.value} @ {signal.entry_price:.5f} | {signal.rationale}")
                        if self.on_signal is not None:
                            self.on_signal(signal)
                if max_ticks is not None and self.stats.ticks_processed >= max_ticks:
                    break
        finally:
            self._running = False
            if book_task is not None:
                book_task.cancel()
                try:
                    await book_task
                except asyncio.CancelledError:
                    pass
            logger.info(f"MicroScalpingService stop. {self.stats.summary()}")
        return self.stats

    def _decision_to_signal(self, decision: ScalpDecision, quote: Quote) -> Optional[TradeSignal]:
        """Konversi keputusan menjadi TradeSignal dengan SL/TP berbasis ATR."""
        atr = self._atr
        if atr is None or atr <= 0:
            # Fallback: gunakan spread sebagai skala minimum
            atr = max(quote.spread_pips * 0.0001, 1e-5)

        digits = 5
        entry = round(float(quote.ask if decision.action == SignalAction.BUY else quote.bid), digits)
        mult = self.strategy.atr_stop_multiplier
        rr = self.strategy.min_risk_reward
        tick = 10.0 ** (-digits)

        if decision.action == SignalAction.BUY:
            sl = round(entry - mult * atr, digits)
        else:
            sl = round(entry + mult * atr, digits)

        sl_dist = round(abs(entry - sl), digits)
        if sl_dist <= 0:
            return None

        tp_dist = sl_dist * rr
        tp = round(entry + tp_dist, digits) if decision.action == SignalAction.BUY else round(entry - tp_dist, digits)
        for _ in range(1000):
            if abs(tp - entry) / sl_dist >= rr - 1e-12:
                break
            tp = round(tp + tick, digits) if decision.action == SignalAction.BUY else round(tp - tick, digits)

        return TradeSignal(
            symbol=self.symbol,
            action=decision.action,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            rationale=decision.reason,
        )

    def stop(self) -> None:
        self._running = False
        self.feed.stop()
