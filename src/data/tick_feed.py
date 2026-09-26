"""
Async Tick Ingestion Layer (Arsitektur Penarikan Data).
Menerapkan spesifikasi bagian B:

    "Data ditarik menggunakan metode Asynchronous Event-Loop agar thread utama
     tidak terblokir saat menunggu tick harga baru."

Arsitektur:
    TickFeed (abstract)  ->  menghasilkan Quote secara event-driven
      |-- MockTickFeed    : simulator deterministik (pengembangan macOS/Linux)
      |-- MT5TickFeed     : MetaTrader5 Python API (Windows VPS)
      |-- CTraderTickFeed : cTrader Open API (Linux/Docker)

CATATAN RISET PENTING (jujur soal keterbatasan platform):

1. **MetaTrader5 Python API BUKAN event-driven.** Ia menyediakan polling
   sinkron (`symbol_info_tick()`). Tidak ada callback/subscribe tick di
   Python API-nya. Karena itu MT5TickFeed melakukan polling di dalam
   `asyncio.to_thread` supaya TIDAK memblokir event loop. Latensi riil
   terbatas pada interval polling + IPC ke terminal MT5, bukan 1-5 ms
   yang diklaim untuk IPC murni.

2. **Depth of Market untuk EURUSD umumnya TIDAK tersedia** di broker retail.
   `mt5.market_book_get()` mengembalikan None kecuali broker mengaktifkan DOM
   dan `market_book_add()` sudah dipanggil. Karena itu `supports_depth`
   adalah properti eksplisit, dan strategi fail-closed bila depth absen.

3. **FIX protocol tidak tersedia untuk akun prop retail.** FIX memerlukan
   hubungan prime brokerage / liquidity provider (LMAX, IB, cTrader). Untuk
   akun retail, jalur realistis adalah MT5 IPC atau cTrader Open API.
"""

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator, Callable, List, Optional

import numpy as np
from loguru import logger

from src.data.market import Quote
from src.data.order_book import BookLevel, OrderBookSnapshot


@dataclass
class TickFeedStats:
    """Metrik kesehatan feed."""
    ticks_received: int = 0
    ticks_dropped: int = 0
    last_tick_utc: Optional[datetime] = None
    started_utc: Optional[datetime] = None
    reconnect_count: int = 0

    @property
    def ticks_per_second(self) -> float:
        if not self.started_utc or not self.last_tick_utc:
            return 0.0
        elapsed = (self.last_tick_utc - self.started_utc).total_seconds()
        return self.ticks_received / elapsed if elapsed > 0 else 0.0


class TickFeed(ABC):
    """
    Kontrak feed tick asynchronous.

    Implementasi WAJIB:
      - tidak memblokir event loop
      - memanggil `on_tick` untuk setiap quote valid
      - fail-closed: kegagalan koneksi tidak boleh menghasilkan tick palsu
    """

    def __init__(self, symbol: str):
        self.symbol = symbol.upper()
        self.stats = TickFeedStats()
        self._running = False

    @property
    @abstractmethod
    def supports_depth(self) -> bool:
        """Apakah feed ini menyediakan order book depth?"""

    @abstractmethod
    async def _next_tick(self) -> Optional[Quote]:
        """Ambil tick berikutnya (non-blocking). None = belum ada data."""

    async def _next_book(self) -> Optional[OrderBookSnapshot]:
        """Ambil snapshot order book. Default: tidak didukung."""
        return None

    async def stream(self, on_tick: Callable[[Quote], None]) -> None:
        """Loop utama: tarik tick dan teruskan ke callback."""
        self._running = True
        self.stats.started_utc = datetime.now(timezone.utc)
        while self._running:
            try:
                quote = await self._next_tick()
                if quote is not None:
                    self.stats.ticks_received += 1
                    self.stats.last_tick_utc = quote.timestamp_utc
                    on_tick(quote)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # fail-closed: log, jangan kirim tick palsu
                self.stats.ticks_dropped += 1
                logger.warning(f"{self.__class__.__name__} tick error: {exc}")
                await asyncio.sleep(0.1)

    async def __aiter__(self) -> AsyncIterator[Quote]:
        self._running = True
        if self.stats.started_utc is None:
            self.stats.started_utc = datetime.now(timezone.utc)
        while self._running:
            q = await self._next_tick()
            if q is not None:
                self.stats.ticks_received += 1
                self.stats.last_tick_utc = q.timestamp_utc
                yield q
            # Yield kontrol ke event loop setiap tick.
            #
            # PENTING: tanpa ini, feed berkecepatan tinggi (tanpa sleep nyata)
            # akan memonopoli event loop sehingga task lain -- misalnya polling
            # order book dan heartbeat -- tidak pernah dijadwalkan (starvation).
            # Ini persis persyaratan spesifikasi: "thread utama tidak terblokir".
            await asyncio.sleep(0)

    def stop(self) -> None:
        self._running = False


class MockTickFeed(TickFeed):
    """
    Feed simulator deterministik untuk pengembangan & pengujian.

    Mendukung dua mode:
      - "random_walk" : harga berjalan acak (default)
      - "mean_reverting": Ornstein-Uhlenbeck (untuk menguji strategi reversion)
    """

    def __init__(
        self,
        symbol: str = "EURUSD",
        start_price: float = 1.08500,
        spread_pips: float = 1.0,
        tick_interval: float = 0.0,
        sigma: float = 0.00002,
        theta: float = 0.0,
        seed: Optional[int] = 42,
        max_ticks: Optional[int] = None,
        virtual_clock_seconds: float = 0.0,
        stress_at_tick: Optional[int] = None,
        stress_sigma_multiplier: float = 12.0,
    ):
        super().__init__(symbol)
        self.start_price = start_price
        self.spread_pips = spread_pips
        self.tick_interval = tick_interval
        self.sigma = sigma
        self.theta = theta
        self._rng = np.random.default_rng(seed)
        self._mid = start_price
        self._emitted = 0
        self.max_ticks = max_ticks
        # Regime switch: setelah `stress_at_tick`, volatilitas dikalikan.
        # Diperlukan untuk menguji circuit breaker secara end-to-end, karena
        # VR = ATR_fast/ATR_slow mengukur PERUBAHAN volatilitas, bukan levelnya.
        # Volatilitas yang tinggi tapi KONSTAN tetap menghasilkan VR ~ 1.0.
        self.stress_at_tick = stress_at_tick
        self.stress_sigma_multiplier = stress_sigma_multiplier
        # Virtual clock: majukan timestamp tiap tick tanpa benar-benar sleep.
        # Diperlukan agar agregasi bar (M1) dapat diuji secara deterministik
        # dan cepat, tanpa menunggu waktu nyata berjalan.
        self.virtual_clock_seconds = virtual_clock_seconds
        self._virtual_now = datetime.now(timezone.utc)

    @property
    def supports_depth(self) -> bool:
        return True

    async def _next_tick(self) -> Optional[Quote]:
        if self.max_ticks is not None and self._emitted >= self.max_ticks:
            self._running = False
            return None

        if self.tick_interval > 0:
            await asyncio.sleep(self.tick_interval)

        # Terapkan rezim volatilitas bila sudah melewati ambang tick
        effective_sigma = self.sigma
        if self.stress_at_tick is not None and self._emitted >= self.stress_at_tick:
            effective_sigma = self.sigma * self.stress_sigma_multiplier

        # Ornstein-Uhlenbeck bila theta > 0, jika tidak random walk
        shock = float(self._rng.normal(0.0, effective_sigma))
        if self.theta > 0:
            self._mid += self.theta * (self.start_price - self._mid) + shock
        else:
            self._mid += shock

        half = (self.spread_pips * 0.0001) / 2.0
        self._emitted += 1

        if self.virtual_clock_seconds > 0:
            from datetime import timedelta
            self._virtual_now = self._virtual_now + timedelta(seconds=self.virtual_clock_seconds)
            ts = self._virtual_now
        else:
            ts = datetime.now(timezone.utc)

        return Quote(
            symbol=self.symbol,
            bid=round(self._mid - half, 5),
            ask=round(self._mid + half, 5),
            spread_pips=self.spread_pips,
            timestamp_utc=ts,
        )

    async def _next_book(self) -> Optional[OrderBookSnapshot]:
        half = (self.spread_pips * 0.0001) / 2.0
        base_vol = 1_000_000.0
        # Imbalance berkorelasi dengan deviasi dari harga awal
        dev = (self._mid - self.start_price) / max(self.sigma, 1e-9)
        ratio = float(np.clip(1.0 + dev * 0.05, 0.2, 5.0))
        bids: List[BookLevel] = []
        asks: List[BookLevel] = []
        for k in range(5):
            vol = base_vol * (0.85 ** k)
            bids.append(BookLevel(self._mid - half - 0.00001 * k, vol * ratio))
            asks.append(BookLevel(self._mid + half + 0.00001 * k, vol))
        return OrderBookSnapshot(self.symbol, tuple(bids), tuple(asks))


class MT5TickFeed(TickFeed):
    """
    Feed MetaTrader 5 (Windows VPS).

    MT5 Python API bersifat POLLING sinkron. Kita jalankan di thread terpisah
    via `asyncio.to_thread` agar event loop tidak terblokir.
    """

    def __init__(
        self,
        symbol: str = "EURUSD",
        poll_interval: float = 0.05,
        login: Optional[int] = None,
        password: Optional[str] = None,
        server: Optional[str] = None,
        path: Optional[str] = None,
        enable_depth: bool = True,
    ):
        super().__init__(symbol)
        self.poll_interval = poll_interval
        self.login = login
        self.password = password
        self.server = server
        self.path = path
        self.enable_depth = enable_depth
        self._mt5 = None
        self._depth_available = False

    @property
    def supports_depth(self) -> bool:
        return self._depth_available

    def connect(self) -> bool:
        try:
            import MetaTrader5 as mt5
            self._mt5 = mt5
        except ImportError:
            logger.error("MetaTrader5 tidak terpasang (hanya tersedia di Windows).")
            return False

        kwargs = {"path": self.path} if self.path else {}
        if not self._mt5.initialize(**kwargs):
            logger.error(f"MT5 initialize gagal: {self._mt5.last_error()}")
            return False
        if self.login is not None:
            if not self._mt5.login(self.login, password=self.password, server=self.server):
                logger.error(f"MT5 login gagal: {self._mt5.last_error()}")
                self._mt5.shutdown()
                return False

        # Coba aktifkan DOM -- jangan asumsikan berhasil!
        if self.enable_depth:
            try:
                self._depth_available = bool(self._mt5.market_book_add(self.symbol))
                if not self._depth_available:
                    logger.warning(
                        f"DOM tidak tersedia untuk {self.symbol}. "
                        "Broker retail umumnya tidak menyediakan Level-2 untuk FX. "
                        "Order book imbalance akan fail-closed."
                    )
            except Exception as exc:
                logger.warning(f"market_book_add gagal: {exc}")
                self._depth_available = False
        return True

    def disconnect(self) -> None:
        if self._mt5:
            if self._depth_available:
                try:
                    self._mt5.market_book_release(self.symbol)
                except Exception:
                    pass
            self._mt5.shutdown()

    def _poll_tick(self) -> Optional[Quote]:
        tick = self._mt5.symbol_info_tick(self.symbol)
        if tick is None:
            return None
        pip = 0.01 if "JPY" in self.symbol else 0.0001
        return Quote(
            symbol=self.symbol,
            bid=float(tick.bid),
            ask=float(tick.ask),
            spread_pips=float((tick.ask - tick.bid) / pip),
            timestamp_utc=datetime.fromtimestamp(tick.time_msc / 1000.0, tz=timezone.utc)
            if getattr(tick, "time_msc", 0) else datetime.now(timezone.utc),
        )

    async def _next_tick(self) -> Optional[Quote]:
        if self._mt5 is None:
            return None
        await asyncio.sleep(self.poll_interval)
        return await asyncio.to_thread(self._poll_tick)

    def _poll_book(self) -> Optional[OrderBookSnapshot]:
        if not self._depth_available:
            return None
        raw = self._mt5.market_book_get(self.symbol)
        if not raw:
            return None
        bids, asks = [], []
        for entry in raw:
            lvl = BookLevel(float(entry.price), float(entry.volume))
            if entry.type == self._mt5.BOOK_TYPE_SELL:
                asks.append(lvl)
            else:
                bids.append(lvl)
        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)
        if not bids or not asks:
            return None
        return OrderBookSnapshot(self.symbol, tuple(bids), tuple(asks))

    async def _next_book(self) -> Optional[OrderBookSnapshot]:
        if self._mt5 is None or not self._depth_available:
            return None
        return await asyncio.to_thread(self._poll_book)


class CTraderTickFeed(TickFeed):
    """
    Feed cTrader Open API (Linux/Docker).

    cTrader Open API adalah protobuf-over-TLS (bukan FIX). Implementasi penuh
    memerlukan pustaka `ctrader-open-api` dan kredensial OAuth. Kelas ini
    menyediakan kerangka + fail-closed bila pustaka tidak tersedia.
    """

    def __init__(self, symbol: str = "EURUSD", **kwargs):
        super().__init__(symbol)
        self._available = False
        try:
            import ctrader_open_api  # noqa: F401
            self._available = True
        except ImportError:
            logger.warning(
                "ctrader-open-api tidak terpasang. CTraderTickFeed tidak aktif. "
                "Pasang dengan: pip install ctrader-open-api"
            )

    @property
    def supports_depth(self) -> bool:
        return False  # cTrader retail umumnya tidak mengekspos L2 penuh

    async def _next_tick(self) -> Optional[Quote]:
        if not self._available:
            await asyncio.sleep(1.0)
            return None
        raise NotImplementedError(
            "CTraderTickFeed memerlukan kredensial OAuth dan implementasi "
            "protobuf handler. Lihat docs/ARCHITECTURE_ALIGNMENT.md."
        )
