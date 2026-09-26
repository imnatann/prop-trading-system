"""
Dynamic Position Sizer with Broker-Native Tick Economics & Hard Risk Cap.
Menghitung ukuran lot presisi berdasarkan toleransi risiko tunai (cash risk),
spesifikasi instrumen broker (SymbolSpec), dan kuantisasi volume step.
Menerapkan aturan pertahanan modal: REJECT order jika lot aman < volume_min broker.
"""

import math
from typing import Dict, Optional
from loguru import logger
from src.broker.models import SymbolSpec


class PositionSizer:
    """
    Kalkulator lot sizing otomatis untuk prop trading.
    Formula:
        Cash Risk ($) = Equity * (Risk % / 100)
        Tick Count = |Entry - SL| / tick_size
        Lot = Cash Risk / (Tick Count * tick_value)
    """

    def __init__(self, min_lot: float = 0.01, max_lot: float = 50.0):
        self.min_lot = min_lot
        self.max_lot = max_lot

    @staticmethod
    def get_pip_size(symbol: str) -> float:
        """Ukuran 1 pip untuk instrumen tertentu (fallback jika SymbolSpec tidak tersedia)."""
        sym = symbol.upper()
        if "JPY" in sym:
            return 0.01
        elif "XAU" in sym or "GOLD" in sym:
            return 0.1
        elif "BTC" in sym or "CRYPTO" in sym:
            return 1.0
        else:
            return 0.0001

    @staticmethod
    def get_pip_value_per_lot(
        symbol: str,
        current_price: Optional[float] = None,
        conversion_rate_to_usd: Optional[float] = None
    ) -> float:
        """
        Menghitung estimasi nilai 1 pip dalam USD untuk 1 Standard Lot (100.000 unit).
        Fallback saat live broker metadata tidak tersedia.
        """
        sym = symbol.upper()

        if "XAU" in sym or "GOLD" in sym:
            return 10.0
        if sym.endswith("USD"):
            return 10.0
        if sym == "USDJPY":
            rate = current_price if current_price and current_price > 0 else 150.0
            return round(1000.0 / rate, 2)
        if sym in ("USDCAD", "USDCHF"):
            rate = current_price if current_price and current_price > 0 else 1.35
            return round(10.0 / rate, 2)
        if sym.endswith("JPY"):
            usdjpy_rate = conversion_rate_to_usd if conversion_rate_to_usd else 150.0
            return round(1000.0 / usdjpy_rate, 2)
        if conversion_rate_to_usd and conversion_rate_to_usd > 0:
            return round(10.0 * conversion_rate_to_usd, 2)

        return 10.0

    def calculate_lot(
        self,
        equity: float,
        risk_pct: float,
        entry_price: float,
        stop_loss: float,
        symbol: str = "EURUSD",
        spec: Optional[SymbolSpec] = None,
        conversion_rate_to_usd: Optional[float] = None,
        allow_sub_min_rounding: bool = False
    ) -> float:
        """
        Menghitung besaran lot yang aman.
        Jika spesifikasi broker (SymbolSpec) diberikan, menggunakan broker-native tick economics.
        
        ATURAN BESAR (P0-4 Hard Risk Cap):
        Jika lot aman yang dihitung < broker volume_min, sistem WAJIB mengembalikan 0.0 (REJECT).
        DILARANG menaikkan lot ke volume_min karena akan menyebabkan actual risk > requested risk!
        """
        if equity <= 0 or risk_pct <= 0:
            return 0.0

        sl_distance = abs(entry_price - stop_loss)
        if sl_distance <= 0:
            return 0.0

        risk_amount_usd = equity * (risk_pct / 100.0)

        # 1. Jalur Broker-Native (SymbolSpec tersedia)
        if spec is not None:
            if spec.tick_size <= 0 or spec.tick_value <= 0:
                logger.error(f"Invalid SymbolSpec for {symbol}: tick_size={spec.tick_size}, tick_value={spec.tick_value}")
                return 0.0

            ticks_at_risk = sl_distance / spec.tick_size
            if ticks_at_risk <= 0:
                return 0.0

            raw_lot = risk_amount_usd / (ticks_at_risk * spec.tick_value)

            # P0-4 Hard Risk Floor: Tolak trade jika lot lebih kecil dari batas minimum broker!
            if raw_lot < spec.volume_min:
                if allow_sub_min_rounding:
                    return spec.volume_min
                logger.warning(
                    f"PositionSizer REJECT [{symbol}]: Safe lot {raw_lot:.4f} < broker volume_min {spec.volume_min}. "
                    f"Risk cap enforced to prevent account over-risking."
                )
                return 0.0

            # Kuantisasi volume_step
            volume_step = spec.volume_step if spec.volume_step > 0 else 0.01
            steps = math.floor((raw_lot - spec.volume_min) / volume_step)
            quantized_lot = round(spec.volume_min + (steps * volume_step), 2)

            if quantized_lot > spec.volume_max:
                quantized_lot = spec.volume_max

            return quantized_lot

        # 2. Jalur Fallback (Simulasi Pip Tradisional)
        pip_size = self.get_pip_size(symbol)
        pip_distance = sl_distance / pip_size
        if pip_distance <= 0:
            return 0.0

        pip_value = self.get_pip_value_per_lot(
            symbol=symbol,
            current_price=entry_price,
            conversion_rate_to_usd=conversion_rate_to_usd
        )

        calculated_lot = risk_amount_usd / (pip_distance * pip_value)
        rounded_lot = round(calculated_lot, 2)

        # P0-4 Hard Risk Floor untuk fallback
        if rounded_lot < self.min_lot:
            if allow_sub_min_rounding:
                return self.min_lot
            logger.warning(
                f"PositionSizer REJECT [{symbol}]: Safe lot {calculated_lot:.4f} < min_lot {self.min_lot}. "
                f"Risk cap enforced."
            )
            return 0.0

        if rounded_lot > self.max_lot:
            return self.max_lot

        return rounded_lot
