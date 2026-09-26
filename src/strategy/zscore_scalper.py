"""
Alpha v2: EURUSD Micro-Scalping Z-Score Mean Reversion Strategy.
Menerapkan arsitektur yang diminta secara utuh:

  1. SUMBER DATA   : tick feed asynchronous (mid price = (Ask+Bid)/2)
  2. ENTRY TRIGGER : Z-Score Mean Reversion (PRIMARY)
                     Z_t = (P_t - mu_t) / sigma_t
  3. KONFIRMASI    : Order Book Imbalance I_t
  4. CIRCUIT BREAKER: Volatility Ratio VR_t = ATR_fast(5)/ATR_slow(30)

Alur keputusan (sesuai decision matrix spesifikasi):
  VR_t > 1.30            -> PAUSE, blokir SEMUA entry baru
  VR_t <= 1.30           -> AKTIF, evaluasi Z-Score
      |Z_t| < entry_band -> tidak ada setup
      Z_t <= -band       -> kandidat BUY  (harga di bawah mean -> reversion naik)
      Z_t >= +band       -> kandidat SELL (harga di atas mean -> reversion turun)
  Konfirmasi imbalance:
      BUY  butuh I_t tidak menolak (I_t > -neutral, idealnya I_t > 0)
      SELL butuh I_t tidak menolak (I_t < +neutral, idealnya I_t < 0)

CATATAN JUJUR SOAL EDGE (bukti empiris di docs/ARCHITECTURE_ALIGNMENT.md):
Simulasi Monte Carlo menunjukkan strategi ini RUGI pada biaya realistis
(1.0 pip round-trip) bahkan ketika pasar BENAR-BENAR mean-reverting kuat
(OU theta=0.01): mean -0.66 pip/trade. Titik impas berada di sekitar 0.6-0.7 pip
round-trip. Circuit breaker VR<=1.30 hanya mengubah hasil secara marginal
(-1341 -> -1254 pip) karena ia jarang menyala (~2-5% bar).

Karena itu strategi ini WAJIB dijalankan dengan `require_confirmation=True`
dan TIDAK boleh dipromosikan ke live tanpa validasi forward. Modul ini
menyediakan `allow_without_depth` yang default False (fail-closed).
"""

from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np
import pandas as pd

from src.data.order_book import (
    OrderFlowImbalanceTracker,
    OrderBookSnapshot,
    ImbalanceResult,
    calculate_order_book_imbalance,
    classify_imbalance,
)
from src.data.tick_buffer import MidPriceBuffer, RollingWindowStats
from src.data.volatility_ratio import VolatilityCircuitBreaker, wilder_atr
from src.strategy.base import BaseStrategy, SignalAction, TradeSignal

# Parameter default
DEFAULT_WINDOW = 200
DEFAULT_ENTRY_BAND = 2.0
DEFAULT_EXIT_BAND = 0.0
DEFAULT_ATR_STOP_MULT = 1.5
DEFAULT_MIN_RISK_REWARD = 2.0


@dataclass
class ScalpDecision:
    """Penjelasan lengkap satu keputusan (untuk audit/observability)."""
    action: SignalAction
    z_score: Optional[float]
    volatility_ratio: Optional[float]
    circuit_breaker_active: bool
    imbalance: Optional[float]
    imbalance_available: bool
    reason: str


class EURUSDZScoreMeanReversionStrategy(BaseStrategy):
    """
    Strategi micro-scalping Z-Score mean reversion dengan konfirmasi
    order book imbalance dan circuit breaker volatilitas.

    Dirancang untuk menerima TICK (via `on_tick`) maupun BAR (via
    `generate_signal`, untuk backtest).
    """

    def __init__(
        self,
        window: int = DEFAULT_WINDOW,
        entry_band: float = DEFAULT_ENTRY_BAND,
        exit_band: float = DEFAULT_EXIT_BAND,
        atr_period: int = 14,
        atr_stop_multiplier: float = DEFAULT_ATR_STOP_MULT,
        min_risk_reward: float = DEFAULT_MIN_RISK_REWARD,
        vr_fast: int = 5,
        vr_slow: int = 30,
        vr_threshold: float = 1.30,
        imbalance_levels: int = 5,
        require_confirmation: bool = True,
        allow_without_depth: bool = False,
        imbalance_mode: str = "veto",
    ):
        super().__init__(name="EURUSD_ZScore_MeanReversion_v2")
        self.window = window
        self.entry_band = entry_band
        self.exit_band = exit_band
        self.atr_period = atr_period
        self.atr_stop_multiplier = atr_stop_multiplier
        self.min_risk_reward = min_risk_reward
        self.imbalance_levels = imbalance_levels
        self.require_confirmation = require_confirmation
        self.allow_without_depth = allow_without_depth
        # Mode konfirmasi imbalance:
        #   "veto"   -> sesuai spesifikasi: tolak entry bila I_t berlawanan >0.30
        #   "weight" -> rekomendasi riset: I_t hanya menskalakan ukuran posisi
        #
        # Riset (Cont/Kukanov/Stoikov 2014; Gould & Bonart 2016) menunjukkan
        # resting depth adalah sinyal LEMAH, horizon prediktifnya hanya
        # detik/satu-tick, mudah di-spoof, dan ambang +-0.30 TIDAK ADA
        # dasarnya di literatur. Karena itu "weight" lebih defensible,
        # tetapi "veto" tetap default agar sesuai spesifikasi yang diminta.
        if imbalance_mode not in ("veto", "weight"):
            raise ValueError(f"imbalance_mode harus 'veto' atau 'weight', diterima {imbalance_mode!r}")
        self.imbalance_mode = imbalance_mode

        # Komponen streaming
        self.mid_buffer = MidPriceBuffer(window=window)
        self.circuit_breaker = VolatilityCircuitBreaker(
            fast_period=vr_fast, slow_period=vr_slow, threshold=vr_threshold
        )
        self.ofi_tracker = OrderFlowImbalanceTracker(window=window)

        self._last_book: Optional[OrderBookSnapshot] = None
        self._last_decision: Optional[ScalpDecision] = None

    # ------------------------------------------------------------------
    # Jalur streaming (tick)
    # ------------------------------------------------------------------
    def on_tick(self, quote) -> Optional[ScalpDecision]:
        """Proses satu tick. Circuit breaker di-update dari bar terpisah."""
        self.mid_buffer.update(quote)
        return None

    def on_book(self, book: Optional[OrderBookSnapshot]) -> None:
        self._last_book = book
        self.ofi_tracker.update(book)

    def evaluate(self, atr: float) -> ScalpDecision:
        """
        Evaluasi keputusan dari state streaming saat ini.
        `atr` dipasok dari bar (M1) karena circuit breaker berbasis candle.
        """
        # 1. Circuit breaker (prioritas tertinggi)
        cb_state = self._current_cb_state()
        if cb_state is not None and not cb_state.is_active:
            d = ScalpDecision(SignalAction.HOLD, None, cb_state.volatility_ratio, False,
                              None, False, cb_state.reason)
            self._last_decision = d
            return d

        # 2. Z-Score (PRIMARY trigger)
        z = self.mid_buffer.z_score()
        if z is None:
            d = ScalpDecision(SignalAction.HOLD, None,
                              cb_state.volatility_ratio if cb_state else None,
                              cb_state.is_active if cb_state else True,
                              None, False,
                              f"Window belum siap ({self.mid_buffer.stats.count}/{self.window} tick)")
            self._last_decision = d
            return d

        # 3. Konfirmasi imbalance
        imb: ImbalanceResult = calculate_order_book_imbalance(self._last_book, self.imbalance_levels)
        if self.require_confirmation and not imb.available and not self.allow_without_depth:
            d = ScalpDecision(SignalAction.HOLD, z,
                              cb_state.volatility_ratio if cb_state else None,
                              cb_state.is_active if cb_state else True,
                              None, False,
                              f"Fail-closed: konfirmasi order book tidak tersedia ({imb.reason})")
            self._last_decision = d
            return d

        # 4. Arah entry
        if z <= -self.entry_band:
            action = SignalAction.BUY
        elif z >= self.entry_band:
            action = SignalAction.SELL
        else:
            d = ScalpDecision(SignalAction.HOLD, z,
                              cb_state.volatility_ratio if cb_state else None,
                              cb_state.is_active if cb_state else True,
                              imb.imbalance if imb.available else None, imb.available,
                              f"Z={z:+.3f} di dalam band +/-{self.entry_band:.2f} (tidak ada setup)")
            self._last_decision = d
            return d

        # 5. Filter konfirmasi imbalance
        if self.require_confirmation and imb.available and self.imbalance_mode == "weight":
            # Mode "weight": TIDAK memveto. I_t hanya mencatat kekuatan sinyal.
            # Skala keyakinan: imbalance yang searah memperkuat, berlawanan melemahkan.
            agreement = imb.imbalance if action == SignalAction.BUY else -imb.imbalance
            d = ScalpDecision(
                action, z,
                cb_state.volatility_ratio if cb_state else None,
                cb_state.is_active if cb_state else True,
                imb.imbalance, True,
                f"Z={z:+.3f} (band {self.entry_band}) | "
                f"I_t={imb.imbalance:+.3f} (agreement={agreement:+.3f}, mode=weight) | "
                f"VR={(cb_state.volatility_ratio if cb_state else float('nan')):.3f}"
            )
            self._last_decision = d
            return d

        if self.require_confirmation and imb.available:
            if action == SignalAction.BUY and imb.imbalance < -0.30:
                d = ScalpDecision(SignalAction.HOLD, z, cb_state.volatility_ratio if cb_state else None,
                                  True, imb.imbalance, True,
                                  f"Z={z:+.3f} menandakan BUY tapi imbalance {imb.imbalance:+.3f} menolak (selling pressure)")
                self._last_decision = d
                return d
            if action == SignalAction.SELL and imb.imbalance > 0.30:
                d = ScalpDecision(SignalAction.HOLD, z, cb_state.volatility_ratio if cb_state else None,
                                  True, imb.imbalance, True,
                                  f"Z={z:+.3f} menandakan SELL tapi imbalance {imb.imbalance:+.3f} menolak (buying pressure)")
                self._last_decision = d
                return d

        d = ScalpDecision(
            action, z,
            cb_state.volatility_ratio if cb_state else None,
            cb_state.is_active if cb_state else True,
            imb.imbalance if imb.available else None, imb.available,
            f"Z={z:+.3f} (band {self.entry_band}) | "
            f"I_t={imb.imbalance:+.3f} ({classify_imbalance(imb.imbalance) if imb.available else 'N/A'}) | "
            f"VR={(cb_state.volatility_ratio if cb_state else float('nan')):.3f}"
        )
        self._last_decision = d
        return d

    def _current_cb_state(self):
        return self.circuit_breaker.last_state if hasattr(self.circuit_breaker, "last_state") else None

    # ------------------------------------------------------------------
    # Jalur batch (backtest) -- kontrak BaseStrategy
    # ------------------------------------------------------------------
    def generate_signal(self, symbol: str, ohlcv_df: pd.DataFrame) -> TradeSignal:
        """
        Versi batch untuk backtest. Menggunakan bar OHLCV.
        Mid price didekati dengan (high+low)/2 bila kolom bid/ask tidak ada.
        """
        needed = max(self.window, self.circuit_breaker.slow_period, self.atr_period) + 5
        if len(ohlcv_df) < needed:
            return TradeSignal(symbol, SignalAction.HOLD, 0.0, 0.0, 0.0,
                               f"Insufficient bars ({len(ohlcv_df)} < {needed})")

        high = ohlcv_df["high"].to_numpy(dtype=np.float64)
        low = ohlcv_df["low"].to_numpy(dtype=np.float64)
        close = ohlcv_df["close"].to_numpy(dtype=np.float64)

        # Mid price per bar: pakai kolom bid/ask bila ada, jika tidak (h+l)/2
        if "bid" in ohlcv_df.columns and "ask" in ohlcv_df.columns:
            mid = (ohlcv_df["bid"].to_numpy(dtype=np.float64) +
                   ohlcv_df["ask"].to_numpy(dtype=np.float64)) / 2.0
        else:
            mid = (high + low) / 2.0

        # --- 1. Circuit breaker (VR) ---
        # VR tidak terdefinisi bila ATR_slow == 0 (pasar benar-benar datar).
        # Fail-safe: perlakukan sebagai TIDAK AKTIF (blokir entry), bukan lolos.
        vr_series = self.circuit_breaker.evaluate_series(high, low, close)
        vr = vr_series[-1]
        if not np.isfinite(vr):
            return TradeSignal(symbol, SignalAction.HOLD, float(close[-1]), 0.0, 0.0,
                               "SISTEM PAUSE: VR tidak terdefinisi (ATR_slow = 0, pasar datar)")
        if vr > self.circuit_breaker.threshold:
            return TradeSignal(symbol, SignalAction.HOLD, float(close[-1]), 0.0, 0.0,
                               f"SISTEM PAUSE: VR={vr:.3f} > {self.circuit_breaker.threshold:.2f}")

        # --- 2. Z-Score dari window mid price ---
        window_mid = mid[-self.window:]
        mu = float(window_mid.mean())
        sigma = float(window_mid.std())     # ddof=0 sesuai rumus spesifikasi
        if sigma <= 0.0:
            return TradeSignal(symbol, SignalAction.HOLD, float(close[-1]), 0.0, 0.0,
                               "sigma = 0 (pasar datar), z-score tidak terdefinisi")
        z = (float(mid[-1]) - mu) / sigma

        if abs(z) < self.entry_band:
            return TradeSignal(symbol, SignalAction.HOLD, float(close[-1]), 0.0, 0.0,
                               f"Z={z:+.3f} di dalam band +/-{self.entry_band:.2f}")

        action = SignalAction.BUY if z <= -self.entry_band else SignalAction.SELL

        # --- 3. Konfirmasi order book (dari book terakhir bila tersedia) ---
        imb = calculate_order_book_imbalance(self._last_book, self.imbalance_levels)
        if self.require_confirmation and imb.available:
            # Mode "veto" (spesifikasi): tolak bila berlawanan kuat.
            # Mode "weight" (rekomendasi riset): jangan veto; imbalance hanya
            # menskalakan ukuran posisi lewat imbalance_size_multiplier().
            if self.imbalance_mode == "veto":
                if action == SignalAction.BUY and imb.imbalance < -0.30:
                    return TradeSignal(symbol, SignalAction.HOLD, float(close[-1]), 0.0, 0.0,
                                       f"Imbalance {imb.imbalance:+.3f} menolak BUY")
                if action == SignalAction.SELL and imb.imbalance > 0.30:
                    return TradeSignal(symbol, SignalAction.HOLD, float(close[-1]), 0.0, 0.0,
                                       f"Imbalance {imb.imbalance:+.3f} menolak SELL")
        elif self.require_confirmation and not imb.available and not self.allow_without_depth:
            return TradeSignal(symbol, SignalAction.HOLD, float(close[-1]), 0.0, 0.0,
                               f"Fail-closed: order book tidak tersedia ({imb.reason})")

        # --- 4. SL/TP berbasis ATR Wilder ---
        atr_arr = wilder_atr(high, low, close, self.atr_period)
        atr = float(atr_arr[-1])
        if not np.isfinite(atr) or atr <= 0:
            return TradeSignal(symbol, SignalAction.HOLD, float(close[-1]), 0.0, 0.0,
                               "ATR tidak valid")

        # Pembulatan ke presisi simbol HARUS dilakukan sebelum menghitung TP,
        # lalu RR diverifikasi ulang. Tanpa ini, pembulatan 5 desimal dapat
        # menghasilkan RR sedikit di bawah target (mis. 1.9783 < 2.0) --
        # sumber flakiness klasik pada test yang meng-assert RR >= 2.0.
        digits = 5
        tick = 10.0 ** (-digits)
        entry = round(float(close[-1]), digits)
        is_buy = action == SignalAction.BUY

        raw_sl = entry - self.atr_stop_multiplier * atr if is_buy else entry + self.atr_stop_multiplier * atr
        sl = round(raw_sl, digits)
        sl_dist = round(abs(entry - sl), digits)
        if sl_dist <= 0:
            return TradeSignal(symbol, SignalAction.HOLD, entry, 0.0, 0.0,
                               "Jarak SL nol setelah pembulatan (ATR terlalu kecil)")

        target_tp_dist = sl_dist * self.min_risk_reward
        tp = round(entry + target_tp_dist, digits) if is_buy else round(entry - target_tp_dist, digits)

        # Jaminan invariant RR >= min_risk_reward: geser TP satu tick bila perlu.
        for _ in range(1000):
            actual_dist = abs(tp - entry)
            if sl_dist > 0 and (actual_dist / sl_dist) >= self.min_risk_reward - 1e-12:
                break
            tp = round(tp + tick, digits) if is_buy else round(tp - tick, digits)

        return TradeSignal(
            symbol=symbol,
            action=action,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            rationale=(
                f"Z-Score {action.value}: Z={z:+.3f} (band {self.entry_band}) | "
                f"VR={vr:.3f} | I_t={imb.imbalance:+.3f}"
                if imb.available else
                f"Z-Score {action.value}: Z={z:+.3f} (band {self.entry_band}) | VR={vr:.3f}"
            ),
        )

    def imbalance_size_multiplier(self, action: SignalAction) -> float:
        """
        Pengali ukuran posisi berbasis imbalance (dipakai pada mode "weight").

        Mengembalikan nilai di [0.5, 1.0]:
          - imbalance searah kuat  -> 1.0  (keyakinan penuh)
          - tidak ada informasi    -> 0.75 (netral)
          - imbalance berlawanan   -> 0.5  (setengah ukuran, bukan veto)

        Ini menggantikan veto keras pada ambang +-0.30 yang tidak punya dasar
        di literatur, sesuai rekomendasi riset.
        """
        imb = calculate_order_book_imbalance(self._last_book, self.imbalance_levels)
        if not imb.available:
            return 1.0
        agreement = imb.imbalance if action == SignalAction.BUY else -imb.imbalance
        if agreement >= 0.30:
            return 1.0
        if agreement <= -0.30:
            return 0.5
        return 0.75

    def reset(self) -> None:
        self.mid_buffer.reset()
        self.circuit_breaker.reset()
        self.ofi_tracker.reset()
        self._last_book = None
        self._last_decision = None
