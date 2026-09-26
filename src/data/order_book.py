"""
Order Book Imbalance (Liquidity Flow & Confirmation).
Menerapkan spesifikasi bagian B:

    I_t = ( sum_{k=1}^{L} V_{k,bid} - sum_{k=1}^{L} V_{k,ask} )
          / ( sum_{k=1}^{L} V_{k,bid} + sum_{k=1}^{L} V_{k,ask} )

    L    : jumlah depth level (misal 5 level teratas)
    V_k  : volume limit order pada level k
    I_t  in [-1.0, +1.0]

Ambang keputusan (sesuai spesifikasi):
    I_t >  +0.30  -> Pembeli mendominasi (buying pressure)
    I_t <  -0.30  -> Penjual mendominasi (selling pressure)
    |I_t| <= 0.10 -> Pasar seimbang / netral

CATATAN PENTING (hasil riset empiris, lihat docs/ARCHITECTURE_ALIGNMENT.md):
Imbalance statis dari resting volume TIDAK sama dengan Order Flow Imbalance (OFI)
yang didefinisikan Cont, Kukanov & Stoikov (2014). OFI -- yaitu PERUBAHAN pada
best bid/ask -- memiliki daya prediksi jangka-pendek yang jauh lebih kuat dan
lebih tahan terhadap spoofing dibanding resting depth statis. Karena itu modul ini
menyediakan KEDUANYA: `imbalance` (statis, sesuai spesifikasi) dan `order_flow_imbalance`
(OFI, sebagai konfirmasi yang lebih robust). Strategi dapat memilih mana yang dipakai.

PENTING: sinyal ini HANYA berfungsi bila feed benar-benar menyediakan depth.
Pada akun retail MT5, Depth of Market untuk EURUSD umumnya TIDAK tersedia
(mt5.market_book_get() mengembalikan None). Modul ini fail-closed: bila depth
tidak tersedia, ia melaporkan available=False dan strategi memperlakukannya
sebagai "tidak ada konfirmasi", bukan sebagai netral.
"""

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np

# Ambang sesuai spesifikasi
BUYING_PRESSURE_THRESHOLD = 0.30
SELLING_PRESSURE_THRESHOLD = -0.30
NEUTRAL_BAND = 0.10


@dataclass(frozen=True)
class BookLevel:
    """Satu level harga pada order book."""
    price: float
    volume: float


@dataclass(frozen=True)
class OrderBookSnapshot:
    """Snapshot order book: bids menurun, asks menaik dari best price."""
    symbol: str
    bids: Tuple[BookLevel, ...]
    asks: Tuple[BookLevel, ...]

    @property
    def is_valid(self) -> bool:
        return len(self.bids) > 0 and len(self.asks) > 0


@dataclass(frozen=True)
class ImbalanceResult:
    """Hasil kalkulasi imbalance."""
    imbalance: float
    available: bool
    levels_used: int
    bid_volume: float = 0.0
    ask_volume: float = 0.0
    reason: str = ""


def calculate_order_book_imbalance(
    book: Optional[OrderBookSnapshot],
    levels: int = 5,
) -> ImbalanceResult:
    """
    Hitung I_t sesuai rumus spesifikasi.

    Fail-closed: bila book tidak tersedia / tidak valid, kembalikan
    available=False. Pemanggil WAJIB memperlakukan ini sebagai
    "konfirmasi tidak tersedia", bukan sebagai pasar netral.
    """
    if book is None:
        return ImbalanceResult(0.0, False, 0, reason="Order book tidak tersedia (DOM tidak di-subscribe / tidak didukung broker)")
    if not book.is_valid:
        return ImbalanceResult(0.0, False, 0, reason="Order book kosong pada salah satu sisi")
    if levels < 1:
        raise ValueError(f"levels harus >= 1, diterima {levels}")

    bid_vol = float(sum(lvl.volume for lvl in book.bids[:levels]))
    ask_vol = float(sum(lvl.volume for lvl in book.asks[:levels]))
    total = bid_vol + ask_vol

    if total <= 0.0:
        return ImbalanceResult(0.0, False, min(levels, len(book.bids), len(book.asks)),
                               reason="Total volume nol")

    imbalance = (bid_vol - ask_vol) / total
    # Clamp ke [-1, 1] untuk proteksi floating point
    imbalance = float(np.clip(imbalance, -1.0, 1.0))

    return ImbalanceResult(
        imbalance=imbalance,
        available=True,
        levels_used=min(levels, len(book.bids), len(book.asks)),
        bid_volume=bid_vol,
        ask_volume=ask_vol,
    )


def classify_imbalance(imbalance: float) -> str:
    """Klasifikasi regime likuiditas sesuai ambang spesifikasi."""
    if imbalance > BUYING_PRESSURE_THRESHOLD:
        return "BUYING_PRESSURE"
    if imbalance < SELLING_PRESSURE_THRESHOLD:
        return "SELLING_PRESSURE"
    if abs(imbalance) <= NEUTRAL_BAND:
        return "NEUTRAL"
    return "MILD"


class OrderFlowImbalanceTracker:
    """
    Order Flow Imbalance (Cont, Kukanov & Stoikov 2014).

    OFI mengukur PERUBAHAN pada best bid/ask, bukan resting depth statis:

        e_n =  I{P^b_n >= P^b_{n-1}} * V^b_n
             - I{P^b_n <= P^b_{n-1}} * V^b_{n-1}
             - I{P^a_n <= P^a_{n-1}} * V^a_n
             + I{P^a_n >= P^a_{n-1}} * V^a_{n-1}

    Di mana V^b, V^a adalah volume pada best bid/ask. Nilai ini diakumulasi
    dalam window rolling dan dinormalisasi.

    Ini lebih robust terhadap spoofing karena mengukur aliran order aktual
    (penambahan/pembatalan) alih-alih kedalaman yang bisa dipalsukan.
    """

    def __init__(self, window: int = 50):
        if window < 2:
            raise ValueError("window harus >= 2")
        self.window = int(window)
        self._prev_best_bid: Optional[BookLevel] = None
        self._prev_best_ask: Optional[BookLevel] = None
        self._events: List[float] = []
        self._cumulative: float = 0.0

    def update(self, book: Optional[OrderBookSnapshot]) -> Optional[float]:
        """Hitung kontribusi OFI dari snapshot baru; kembalikan OFI ternormalisasi."""
        if book is None or not book.is_valid:
            return self._normalized()

        best_bid = book.bids[0]
        best_ask = book.asks[0]

        if self._prev_best_bid is None or self._prev_best_ask is None:
            self._prev_best_bid, self._prev_best_ask = best_bid, best_ask
            return None

        pb, pb_prev = best_bid.price, self._prev_best_bid.price
        vb, vb_prev = best_bid.volume, self._prev_best_bid.volume
        pa, pa_prev = best_ask.price, self._prev_best_ask.price
        va, va_prev = best_ask.volume, self._prev_best_ask.volume

        e = 0.0
        # Sisi BID
        if pb >= pb_prev:
            e += vb
        if pb <= pb_prev:
            e -= vb_prev
        # Sisi ASK
        if pa <= pa_prev:
            e -= va
        if pa >= pa_prev:
            e += va_prev

        self._events.append(e)
        if len(self._events) > self.window:
            self._events.pop(0)

        self._prev_best_bid, self._prev_best_ask = best_bid, best_ask
        self._cumulative += e
        return self._normalized()

    def _normalized(self) -> Optional[float]:
        """Normalisasi OFI ke [-1, 1] memakai total volume absolut dalam window."""
        if not self._events:
            return None
        arr = np.asarray(self._events, dtype=np.float64)
        denom = float(np.abs(arr).sum())
        if denom <= 0.0:
            return 0.0
        return float(np.clip(arr.sum() / denom, -1.0, 1.0))

    @property
    def cumulative(self) -> float:
        return self._cumulative

    def reset(self) -> None:
        self._prev_best_bid = None
        self._prev_best_ask = None
        self._events.clear()
        self._cumulative = 0.0


def make_synthetic_book(
    mid: float,
    levels: int = 5,
    tick_size: float = 0.00001,
    base_volume: float = 1_000_000.0,
    bid_ask_ratio: float = 1.0,
    decay: float = 0.85,
) -> OrderBookSnapshot:
    """
    Bangun order book sintetis untuk pengujian.
    bid_ask_ratio > 1 => lebih banyak volume di sisi bid (buying pressure).
    """
    bids: List[BookLevel] = []
    asks: List[BookLevel] = []
    for k in range(levels):
        vol = base_volume * (decay ** k)
        bids.append(BookLevel(price=mid - tick_size * (k + 1), volume=vol * bid_ask_ratio))
        asks.append(BookLevel(price=mid + tick_size * (k + 1), volume=vol))
    return OrderBookSnapshot(symbol="EURUSD", bids=tuple(bids), asks=tuple(asks))
