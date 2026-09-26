"""
Broker Domain Models & Symbol Specifications.
Mendefinisikan spesifikasi instrumen broker-native untuk kalkulasi risiko presisi.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SymbolSpec:
    """
    Spesifikasi instrumen dari broker (MetaTrader 5 / cTrader).
    Menghilangkan dependensi hardcode pip size / pip value.
    """
    symbol: str
    tick_size: float          # Ukuran pergerakan harga terkecil (misal 0.00001 untuk 5-digit EURUSD, 0.001 untuk JPY, 0.01 untuk XAUUSD)
    tick_value: float         # Nilai uang 1 tick dalam mata uang deposit (USD) untuk 1.0 contract size
    contract_size: float      # Besaran unit kontrak (misal 100,000 unit untuk Forex, 100 oz untuk Gold)
    volume_min: float         # Minimum lot yang diizinkan broker (misal 0.01)
    volume_max: float         # Maksimum lot yang diizinkan broker (misal 50.0 atau 100.0)
    volume_step: float        # Kenaikan lot bertahap (misal 0.01)
    digits: int               # Jumlah desimal harga (misal 5 untuk EURUSD, 3 untuk USDJPY, 2 untuk XAUUSD)
    spread_limit_pips: Optional[float] = 3.0

    @property
    def pip_size(self) -> float:
        """
        Ukuran 1 pip standar (10 ticks untuk 3/5 digit broker).
        Forex 5-digit: tick_size 0.00001 -> pip_size 0.0001
        Forex 3-digit JPY: tick_size 0.001 -> pip_size 0.01
        Gold 2-digit: tick_size 0.01 -> pip_size 0.1
        """
        if self.digits in (3, 5):
            return self.tick_size * 10.0
        return self.tick_size

    @property
    def pip_value(self) -> float:
        """Nilai 1 pip untuk 1 standard lot = tick_value * 10 (pada broker 3/5 digit)."""
        if self.digits in (3, 5):
            return self.tick_value * 10.0
        return self.tick_value
