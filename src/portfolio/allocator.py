"""
Portfolio Allocator & Multi-Instrument Risk Budgeting.
Mengontrol alokasi modal antar instrumen (EURUSD, USDJPY, GBPUSD, XAUUSD)
dan membatasi korelasi arah eksposur mata uang.
"""

from typing import List, Optional, Set, Tuple
from loguru import logger

from src.broker.base import PositionInfo
from src.portfolio.exposure import CurrencyExposureReport, PortfolioExposureManager


class PortfolioAllocator:
    """Alokator portofolio prop trading dengan pembatasan instrumen dan konsentrasi mata uang."""

    def __init__(
        self,
        allowed_symbols: Optional[Set[str]] = None,
        max_exposure_per_currency_usd: float = 250_000.0,
        max_portfolio_open_positions: int = 3
    ):
        # Default target roadmap per Executive Decision:
        # V0 Primary: EURUSD
        # Portfolio Expansion #1: USDJPY
        # Portfolio Expansion #2: GBPUSD
        # Gold: XAUUSD
        self.allowed_symbols = allowed_symbols or {"EURUSD", "USDJPY", "GBPUSD"}
        self.exposure_manager = PortfolioExposureManager(max_exposure_per_currency_usd)
        self.max_portfolio_open_positions = max_portfolio_open_positions

    def evaluate_allocation(
        self,
        symbol: str,
        action: str,
        lot_size: float,
        current_positions: List[PositionInfo]
    ) -> Tuple[bool, str, float]:
        """
        Mengevaluasi apakah sinyal trade baru diizinkan oleh kebijakan alokasi portofolio.
        Returns: (is_approved, reason, allocated_lot)
        """
        sym = symbol.upper()

        # 1. Cek instrumen resmi yang diizinkan
        if sym not in self.allowed_symbols:
            msg = f"Symbol {sym} is not in approved portfolio instruments: {list(self.allowed_symbols)}"
            logger.warning(f"PortfolioAllocator: {msg}")
            return False, msg, 0.0

        # 2. Cek total batas posisi terbuka portofolio
        if len(current_positions) >= self.max_portfolio_open_positions:
            msg = f"Max portfolio positions reached: {len(current_positions)} >= {self.max_portfolio_open_positions}"
            logger.warning(f"PortfolioAllocator: {msg}")
            return False, msg, 0.0

        # 3. Simulasikan penambahan posisi untuk mengecek konsentrasi mata uang
        simulated_pos = PositionInfo(
            position_id="SIMULATED",
            client_order_id="SIMULATED",
            symbol=sym,
            action=action.upper(),
            volume=lot_size,
            entry_price=1.0,
            sl=0.0,
            tp=0.0,
            current_price=1.0,
            profit=0.0
        )
        simulated_list = list(current_positions) + [simulated_pos]
        exposure_rep = self.exposure_manager.decompose_positions(simulated_list)

        if exposure_rep.is_concentrated:
            msg = (
                f"Trade rejected: Would cause excessive {exposure_rep.dominant_currency} concentration "
                f"(${exposure_rep.max_single_currency_exposure_usd:,.2f} > limit ${self.exposure_manager.max_exposure_per_currency_usd:,.2f})"
            )
            logger.warning(f"PortfolioAllocator: {msg}")
            return False, msg, 0.0

        return True, "Approved by Portfolio Allocator", lot_size
