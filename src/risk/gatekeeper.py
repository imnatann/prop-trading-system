"""
Unified Risk Gatekeeper (The Pre-Trade Gatekeeper).
Memvalidasi setiap sinyal sebelum diteruskan ke Execution Engine.
"""

from dataclasses import dataclass
from typing import List, Optional
from loguru import logger

from config.prop_rules import PropFirmRules
from src.strategy.base import TradeSignal, SignalAction
from src.data.news_calendar import EconomicEvent
from src.broker.models import SymbolSpec
from .drawdown_monitor import DrawdownMonitor, AccountSnapshot
from .position_sizer import PositionSizer
from .news_filter import NewsFilter


@dataclass
class RiskDecision:
    is_approved: bool
    lot_size: float
    rejection_reason: Optional[str] = None


class RiskGatekeeper:
    """Gerbang otorisasi utama risiko untuk akun prop firm."""

    def __init__(self, rules: PropFirmRules, drawdown_monitor: DrawdownMonitor):
        self.rules = rules
        self.drawdown_monitor = drawdown_monitor
        self.position_sizer = PositionSizer()
        self.news_filter = NewsFilter(
            minutes_before=rules.news_avoid_minutes_before,
            minutes_after=rules.news_avoid_minutes_after
        )

    def evaluate(
        self,
        signal: TradeSignal,
        account: AccountSnapshot,
        open_positions_count: int,
        current_spread_pips: float,
        economic_events: List[EconomicEvent],
        spec: Optional[SymbolSpec] = None
    ) -> RiskDecision:
        """
        Mengevaluasi sinyal trading secara komprehensif.
        Menolak jika ada satupun aturan prop firm yang berpotensi dilanggar.
        """
        # 1. Validasi aksi sinyal
        if signal.action == SignalAction.HOLD:
            return RiskDecision(is_approved=False, lot_size=0.0, rejection_reason="Signal is HOLD")

        # 2. Cek Daily Drawdown Limit
        daily_loss_pct = self.drawdown_monitor.calculate_daily_loss_pct(account.equity)
        if daily_loss_pct >= self.rules.max_daily_loss_pct:
            reason = f"Daily Drawdown breach: {daily_loss_pct:.2f}% >= limit {self.rules.max_daily_loss_pct:.2f}%"
            logger.warning(f"Risk Gatekeeper REJECT: {reason}")
            return RiskDecision(is_approved=False, lot_size=0.0, rejection_reason=reason)

        # 3. Cek Total Drawdown Limit
        total_loss_pct = self.drawdown_monitor.calculate_total_loss_pct(account.equity)
        if total_loss_pct >= self.rules.max_total_loss_pct:
            reason = f"Total Drawdown breach: {total_loss_pct:.2f}% >= limit {self.rules.max_total_loss_pct:.2f}%"
            logger.warning(f"Risk Gatekeeper REJECT: {reason}")
            return RiskDecision(is_approved=False, lot_size=0.0, rejection_reason=reason)

        # 4. Cek Batas Maksimal Order Aktif
        if open_positions_count >= self.rules.max_open_trades:
            reason = f"Max open positions reached: {open_positions_count} >= {self.rules.max_open_trades}"
            logger.warning(f"Risk Gatekeeper REJECT: {reason}")
            return RiskDecision(is_approved=False, lot_size=0.0, rejection_reason=reason)

        # 5. Cek Spread Filter
        if current_spread_pips > self.rules.max_spread_pips:
            reason = f"Spread too high: {current_spread_pips:.1f} pips > max {self.rules.max_spread_pips:.1f} pips"
            logger.warning(f"Risk Gatekeeper REJECT: {reason}")
            return RiskDecision(is_approved=False, lot_size=0.0, rejection_reason=reason)

        # 6. Cek News Blackout Window
        is_news_blocked, news_reason = self.news_filter.is_in_news_blackout(
            symbol=signal.symbol,
            events=economic_events
        )
        if is_news_blocked:
            logger.warning(f"Risk Gatekeeper REJECT (News): {news_reason}")
            return RiskDecision(is_approved=False, lot_size=0.0, rejection_reason=news_reason)

        # 7. Kalkulasi Lot Sizing Terukur (Broker-Native & Hard Risk Cap)
        lot_size = self.position_sizer.calculate_lot(
            equity=account.equity,
            risk_pct=self.rules.risk_per_trade_pct,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            symbol=signal.symbol,
            spec=spec
        )

        if lot_size <= 0:
            reason = "Calculated safe lot size is 0 (below broker volume_min or invalid SL)"
            logger.warning(f"Risk Gatekeeper REJECT: {reason}")
            return RiskDecision(is_approved=False, lot_size=0.0, rejection_reason=reason)

        logger.info(
            f"Risk Gatekeeper APPROVED: {signal.action} {signal.symbol} | "
            f"Lot: {lot_size} | Daily DD: {daily_loss_pct:.2f}% | SL: {signal.stop_loss} | TP: {signal.take_profit}"
        )
        return RiskDecision(is_approved=True, lot_size=lot_size, rejection_reason=None)
