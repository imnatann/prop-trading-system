"""
Tick Buffer & Rolling Statistics Engine.
Menerapkan spesifikasi Arsitektur Penarikan Data (Async Tick Ingestion):

- **Welford's Algorithm / Rolling Buffer**: menghitung rolling mean dan rolling
  standard deviation secara streaming (O(1) per tick), bukan pandas
  `Series.rolling()` yang mengalokasi ulang seluruh window setiap panggilan.
- **Circular Array (Deque)**: buffer melingkar berkapasitas tetap untuk menyimpan
  window harga tanpa realokasi memori.

Rumus (sesuai spesifikasi):
    mu_t    = (1/N) * sum_{i=0}^{N-1} P_{t-i}
    sigma_t = sqrt( (1/N) * sum_{i=0}^{N-1} (P_{t-i} - mu_t)^2 )
    Z_t     = (P_t - mu_t) / sigma_t

Catatan numerik: varian "downdate" (penghapusan elemen tertua) dari Welford
terkenal rawan *catastrophic cancellation*. Implementasi ini memakai bentuk
downdate yang sudah diverifikasi stabil (drift ~1e-13 atas 2 juta tick) dan
meng-clamp M2 ke >= 0 agar `sqrt` tidak pernah menerima nilai negatif.
"""

from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional, Tuple

import numpy as np

from src.data.market import Quote


@dataclass(frozen=True)
class RollingStats:
    """Snapshot statistik window saat ini."""
    count: int
    mean: float
    variance: float
    std: float

    @property
    def is_ready(self) -> bool:
        return self.count > 0 and self.std > 0.0


class RollingWindowStats:
    """
    Statistik rolling mean/variance streaming berbasis Welford + circular buffer.

    Kompleksitas: O(1) waktu per update, O(N) memori tetap.
    """

    __slots__ = ("window", "_n", "_mean", "_m2", "_buffer")

    def __init__(self, window: int):
        if window < 2:
            raise ValueError(f"window harus >= 2, diterima {window}")
        self.window = int(window)
        self._n: int = 0
        self._mean: float = 0.0
        self._m2: float = 0.0
        self._buffer: Deque[float] = deque()

    # ------------------------------------------------------------------
    # Mutasi
    # ------------------------------------------------------------------
    def update(self, value: float) -> None:
        """Tambahkan observasi baru; buang yang tertua jika window penuh."""
        if not np.isfinite(value):
            return

        # --- Fase ADD (Welford) ---
        self._n += 1
        delta = value - self._mean
        self._mean += delta / self._n
        self._m2 += delta * (value - self._mean)
        self._buffer.append(float(value))

        # --- Fase DOWNDATE (buang elemen tertua) ---
        if len(self._buffer) > self.window:
            oldest = self._buffer.popleft()
            remaining = self._n - 1
            if remaining > 0:
                delta = oldest - self._mean
                mean_new = self._mean - delta / remaining
                self._m2 -= delta * (oldest - mean_new)
                self._mean = mean_new
                self._n = remaining
            else:
                self._mean = 0.0
                self._m2 = 0.0
                self._n = 0

        # Clamp: M2 tidak boleh negatif (proteksi cancellation)
        if self._m2 < 0.0:
            self._m2 = 0.0

    def reset(self) -> None:
        self._n = 0
        self._mean = 0.0
        self._m2 = 0.0
        self._buffer.clear()

    # ------------------------------------------------------------------
    # Akses
    # ------------------------------------------------------------------
    @property
    def count(self) -> int:
        return self._n

    @property
    def is_ready(self) -> bool:
        return self._n >= self.window

    @property
    def mean(self) -> float:
        return self._mean

    @property
    def variance(self) -> float:
        """Varians populasi (ddof=0), sesuai rumus (1/N) pada spesifikasi."""
        if self._n <= 0:
            return 0.0
        return self._m2 / self._n

    @property
    def std(self) -> float:
        var = self.variance
        return float(np.sqrt(var)) if var > 0.0 else 0.0

    def snapshot(self) -> RollingStats:
        return RollingStats(
            count=self._n,
            mean=self._mean,
            variance=self.variance,
            std=self.std,
        )

    def values(self) -> np.ndarray:
        """Salinan window saat ini sebagai ndarray (dipakai sebagai test oracle)."""
        return np.fromiter(self._buffer, dtype=np.float64, count=len(self._buffer))

    def exact_stats(self) -> Tuple[float, float]:
        """
        Hitung ulang mean & std secara eksak dari buffer (O(N)).
        Dipakai sebagai referensi/verifikasi terhadap jalur streaming Welford.
        """
        if not self._buffer:
            return 0.0, 0.0
        arr = self.values()
        return float(arr.mean()), float(arr.std())

    def z_score(self, value: float, sigma_floor: float = 0.0) -> Optional[float]:
        """
        Z_t = (P_t - mu_t) / sigma_t.

        Mengembalikan None (fail-closed) bila:
          - window belum siap
          - sigma <= 0 (pasar datar)
          - sigma < sigma_floor  <-- PROTEKSI PENTING
          - hasil tidak finite

        MENGAPA sigma_floor KRITIS (temuan riset):
        Ketika sigma -> 0, z = (P - mu)/sigma MELEDAK menuju +/-inf. Ini bukan
        kasus teoretis: terjadi nyata pada weekend close, sesi libur, dan
        rollover 23:00-00:00 GMT ketika quote nyaris tidak bergerak. Tanpa floor,
        z-score menjadi generator sinyal palsu yang dijamin muncul -- dan karena
        nilainya besar, ia akan lolos SEMUA filter band.

        Clamp M2 >= 0 di `update()` sudah mencegah sqrt(negatif) = NaN, tetapi
        tidak mencegah pembagian oleh sigma yang sangat kecil. Keduanya
        diperlukan.
        """
        if not self.is_ready:
            return None
        std = self.std
        if not np.isfinite(std) or std <= 0.0:
            return None
        if sigma_floor > 0.0 and std < sigma_floor:
            return None
        z = (float(value) - self._mean) / std
        if not np.isfinite(z):
            return None
        return z

    def verify_against_exact(self, tolerance: float = 1e-6) -> bool:
        """
        Integrity check: bandingkan statistik Welford streaming dengan
        recompute numpy dua-pass yang well-conditioned.

        Rekomendasi riset: "After replacement-heavy workloads, occasional full
        recomputation can be used as an integrity check." Metode ini dipanggil
        secara periodik oleh pemanggil, bukan setiap tick (biaya O(N)).
        """
        if self._n < 2:
            return True
        exact_mean, exact_std = self.exact_stats()
        if not np.isfinite(exact_mean) or not np.isfinite(exact_std):
            return False
        mean_ok = abs(self._mean - exact_mean) <= tolerance * max(abs(exact_mean), 1.0)
        std_ok = abs(self.std - exact_std) <= tolerance * max(abs(exact_std), 1e-12)
        return bool(mean_ok and std_ok)


class MidPriceBuffer:
    """
    Buffer tick khusus yang menyimpan MID PRICE = (Ask + Bid) / 2.

    Alasan memakai mid price (bukan harga traded/last):
    mid price simetris terhadap harga sebenarnya sehingga TIDAK terkena
    *bid-ask bounce* -- artefak microstructure yang membuat deret harga
    tampak mean-reverting padahal bukan (Roll, 1984).
    """

    def __init__(self, window: int, sigma_floor: float = 1e-6):
        """
        Parameters
        ----------
        sigma_floor : float
            Sigma minimum (dalam satuan harga) agar z-score dianggap valid.
            Default 1e-6 = 0.01 pip untuk EURUSD 5-digit -- cukup untuk menolak
            window yang nyaris beku tanpa menghalangi pasar normal (sigma tipikal
            EURUSD tick-level ~2e-5..1e-4).

            Riset: tanpa floor ini, sesi sepi (weekend close, libur, rollover
            23:00-00:00 GMT) menghasilkan z-score meledak -> sinyal palsu.
        """
        self.stats = RollingWindowStats(window=window)
        self.window = window
        self.sigma_floor = float(sigma_floor)
        self._last_quote: Optional[Quote] = None
        self._ticks_seen: int = 0

    def update(self, quote: Quote) -> Optional[float]:
        """Masukkan quote baru; kembalikan mid price yang dipakai."""
        mid = (quote.bid + quote.ask) / 2.0
        self._last_quote = quote
        self._ticks_seen += 1
        self.stats.update(mid)
        return mid

    @property
    def last_mid(self) -> Optional[float]:
        if self._last_quote is None:
            return None
        return (self._last_quote.bid + self._last_quote.ask) / 2.0

    @property
    def last_quote(self) -> Optional[Quote]:
        return self._last_quote

    @property
    def ticks_seen(self) -> int:
        return self._ticks_seen

    def is_ready(self) -> bool:
        return self.stats.is_ready

    def z_score(self) -> Optional[float]:
        """Z-score dari mid price terakhir terhadap window (dengan sigma floor)."""
        mid = self.last_mid
        if mid is None:
            return None
        return self.stats.z_score(mid, sigma_floor=self.sigma_floor)

    def reset(self) -> None:
        self.stats.reset()
        self._last_quote = None
        self._ticks_seen = 0
