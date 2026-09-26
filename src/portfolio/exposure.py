"""
Currency Exposure Decomposition & Portfolio Concentration Risk.
Memecah posisi multi-pair ke eksposur mata uang tunggal (misal USD, EUR, GBP, JPY)
untuk mencegah penumpukan risiko tersembunyi (hidden correlated USD shock).
"""

from dataclasses import dataclass, field
from typing import Dict, List
from loguru import logger

from src.broker.base import PositionInfo


@dataclass
class CurrencyExposureReport:
    currency_exposures_usd: Dict[str, float] = field(default_factory=dict)
    max_single_currency_exposure_usd: float = 0.0
    dominant_currency: str = ""
    is_concentrated: bool = False
    warning: str = ""


class PortfolioExposureManager:
    """Manajer konsentrasi eksposur mata uang antar-pasangan valuta."""

    def __init__(self, max_exposure_per_currency_usd: float = 250_000.0):
        self.max_exposure_per_currency_usd = max_exposure_per_currency_usd

    def decompose_positions(self, positions: List[PositionInfo]) -> CurrencyExposureReport:
        """
        Dekomposisi posisi ke nilai nosional USD per mata uang:
        - BUY EURUSD 1.0 lot ($100k): +100k EUR, -100k USD
        - SELL EURUSD 1.0 lot: -100k EUR, +100k USD
        - BUY USDJPY 1.0 lot: +100k USD, -100k JPY
        - BUY GBPUSD 1.0 lot: +100k GBP, -100k USD
        """
        exposures: Dict[str, float] = {}

        for p in positions:
            sym = p.symbol.upper()
            if len(sym) < 6:
                continue

            base = sym[:3]
            quote = sym[3:6]
            notional_base_usd = p.volume * 100_000.0  # Estimasi nosional standar

            is_buy = (p.action.upper() == "BUY")

            # Arah eksposur
            base_dir = 1.0 if is_buy else -1.0
            quote_dir = -1.0 if is_buy else 1.0

            exposures[base] = exposures.get(base, 0.0) + (base_dir * notional_base_usd)
            exposures[quote] = exposures.get(quote, 0.0) + (quote_dir * notional_base_usd)

        # Cari mata uang paling dominan
        dominant_curr = ""
        max_exp = 0.0
        for curr, val in exposures.items():
            abs_val = abs(val)
            if abs_val > max_exp:
                max_exp = abs_val
                dominant_curr = curr

        is_concentrated = max_exp > self.max_exposure_per_currency_usd
        warning = ""
        if is_concentrated:
            warning = (
                f"Concentration breach: {dominant_curr} net exposure ${max_exp:,.2f} "
                f"exceeds ceiling ${self.max_exposure_per_currency_usd:,.2f}"
            )
            logger.warning(f"PortfolioExposureManager: {warning}")

        return CurrencyExposureReport(
            currency_exposures_usd=exposures,
            max_single_currency_exposure_usd=max_exp,
            dominant_currency=dominant_curr,
            is_concentrated=is_concentrated,
            warning=warning
        )
