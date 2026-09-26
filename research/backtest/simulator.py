"""
Realistic Event-Driven Backtesting Simulator for Prop Firm Rules.
Menjalankan bar-by-bar backtest dengan friksi komisi, spread, slippage,
dan pemantauan aturan drawdown harian/total prop firm secara real-time.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import pandas as pd
from loguru import logger

from config.prop_rules import PropFirmRules
from src.broker.models import SymbolSpec
from src.risk.drawdown_monitor import DrawdownMonitor
from src.risk.position_sizer import PositionSizer
from src.strategy.base import BaseStrategy, SignalAction, TradeSignal
from research.backtest.costs import CostModel


@dataclass
class BacktestTrade:
    trade_id: int
    symbol: str
    action: str
    entry_time: Any
    exit_time: Any
    entry_price: float
    exit_price: float
    lot_size: float
    pnl: float
    commission: float
    slippage_cost: float
    exit_reason: str                    # TP, SL, BREACH_HALT, EOD


@dataclass
class BacktestResult:
    initial_balance: float
    final_balance: float
    net_profit: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    profit_factor: float
    max_daily_drawdown_pct: float
    max_total_drawdown_pct: float
    passed_prop_rules: bool
    breach_reason: Optional[str] = None
    trades: List[BacktestTrade] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)


class PropBacktestSimulator:
    """Simulator pengujian strategi dengan penegakan aturan prop firm."""

    def __init__(
        self,
        rules: Optional[PropFirmRules] = None,
        cost_model: Optional[CostModel] = None,
        spec: Optional[SymbolSpec] = None,
        initial_balance: float = 100_000.0,
        risk_per_trade_pct: float = 0.5
    ):
        self.rules = rules or PropFirmRules()
        self.costs = cost_model or CostModel()
        self.spec = spec or SymbolSpec(
            symbol="EURUSD",
            tick_size=0.00001,
            tick_value=1.0,
            contract_size=100000.0,
            volume_min=0.01,
            volume_max=50.0,
            volume_step=0.01,
            digits=5
        )
        self.initial_balance = initial_balance
        self.risk_per_trade_pct = risk_per_trade_pct
        self.sizer = PositionSizer()

    def run(self, strategy: BaseStrategy, df: pd.DataFrame, symbol: str = "EURUSD") -> BacktestResult:
        """
        Menjalankan simulasi bar-by-bar.
        df harus memiliki index datetime dan kolom: open, high, low, close.
        """
        balance = self.initial_balance
        equity = balance
        start_of_day_baseline = balance
        current_day = None

        open_position: Optional[Dict[str, Any]] = None
        trades: List[BacktestTrade] = []
        equity_curve: List[float] = [equity]

        max_daily_dd = 0.0
        max_total_dd = 0.0
        passed_rules = True
        breach_reason = None
        trade_counter = 1

        pip_value = 10.0  # Standard $10 per pip per standard lot for EURUSD
        pip_unit = 0.0001

        for i in range(len(df)):
            idx = df.index[i]
            bar = df.iloc[i]
            bar_date = idx.date() if hasattr(idx, 'date') else idx

            # 1. Midnight rollover: Reset baseline harian
            if current_day != bar_date:
                current_day = bar_date
                # Prop firm standard: max(balance, equity) at midnight
                start_of_day_baseline = max(balance, equity)

            # 2. Periksa Open Position terhadap bar saat ini (SL / TP check)
            if open_position:
                pos = open_position
                action = pos["action"]
                sl = pos["sl"]
                tp = pos["tp"]
                lot = pos["lot"]
                entry_px = pos["entry_price"]

                exit_price = None
                exit_reason = None

                if action == SignalAction.BUY:
                    # Low bar hits Stop Loss
                    if bar["low"] <= sl:
                        exit_price = sl - (self.costs.slippage_pips * pip_unit)
                        exit_reason = "SL"
                    # High bar hits Take Profit
                    elif bar["high"] >= tp:
                        exit_price = tp + (self.costs.slippage_pips * pip_unit)
                        exit_reason = "TP"
                elif action == SignalAction.SELL:
                    # High bar hits Stop Loss
                    if bar["high"] >= sl:
                        exit_price = sl + (self.costs.slippage_pips * pip_unit)
                        exit_reason = "SL"
                    # Low bar hits Take Profit
                    elif bar["low"] <= tp:
                        exit_price = tp - (self.costs.slippage_pips * pip_unit)
                        exit_reason = "TP"

                if exit_price is not None:
                    # Tutup posisi
                    if action == SignalAction.BUY:
                        pips = (exit_price - entry_px) / pip_unit
                    else:
                        pips = (entry_px - exit_price) / pip_unit

                    gross_pnl = pips * pip_value * lot
                    exit_comm = self.costs.compute_exit_costs(lot, pip_value)
                    net_pnl = gross_pnl - exit_comm

                    balance += net_pnl
                    equity = balance

                    trades.append(BacktestTrade(
                        trade_id=trade_counter,
                        symbol=symbol,
                        action=action.value,
                        entry_time=pos["entry_time"],
                        exit_time=idx,
                        entry_price=entry_px,
                        exit_price=exit_price,
                        lot_size=lot,
                        pnl=net_pnl,
                        commission=pos["entry_comm"] + exit_comm,
                        slippage_cost=2 * (self.costs.slippage_pips * pip_value * lot),
                        exit_reason=exit_reason
                    ))
                    trade_counter += 1
                    open_position = None

            # 3. Hitung Floating Equity jika ada posisi terbuka
            if open_position:
                pos = open_position
                curr_price = bar["close"]
                if pos["action"] == SignalAction.BUY:
                    floating_pips = (curr_price - pos["entry_price"]) / pip_unit
                else:
                    floating_pips = (pos["entry_price"] - curr_price) / pip_unit
                floating_pnl = floating_pips * pip_value * pos["lot"]
                equity = balance + floating_pnl
            else:
                equity = balance

            # 4. Evaluasi Drawdown Prop Firm
            daily_dd = ((start_of_day_baseline - equity) / start_of_day_baseline) * 100.0 if start_of_day_baseline > 0 else 0.0
            if daily_dd > max_daily_dd:
                max_daily_dd = daily_dd

            total_dd = ((self.initial_balance - equity) / self.initial_balance) * 100.0 if self.initial_balance > 0 else 0.0
            if total_dd > max_total_dd:
                max_total_dd = total_dd

            if daily_dd >= self.rules.max_daily_loss_pct:
                passed_rules = False
                breach_reason = f"Breached Max Daily Drawdown ({daily_dd:.2f}% >= {self.rules.max_daily_loss_pct}%) at {idx}"
                equity_curve.append(equity)
                break

            if total_dd >= self.rules.max_total_loss_pct:
                passed_rules = False
                breach_reason = f"Breached Max Total Drawdown ({total_dd:.2f}% >= {self.rules.max_total_loss_pct}%) at {idx}"
                equity_curve.append(equity)
                break

            equity_curve.append(equity)

            # 5. Cek Sinyal Strategi jika sedang tidak ada open position
            # Minimum lookback 20 bar
            if open_position is None and i >= 20:
                df_slice = df.iloc[: i + 1]
                signal = strategy.generate_signal(symbol, df_slice)
                if signal.action in (SignalAction.BUY, SignalAction.SELL):
                    # Hitung ukuran lot aman dengan PositionSizer
                    calculated_lot = self.sizer.calculate_lot(
                        equity=balance,
                        risk_pct=self.risk_per_trade_pct,
                        entry_price=signal.entry_price,
                        stop_loss=signal.stop_loss,
                        symbol=symbol,
                        spec=self.spec
                    )

                    if calculated_lot >= self.spec.volume_min:
                        # Masuk posisi dengan friksi spread & slippage
                        entry_comm = self.costs.compute_entry_costs(calculated_lot, pip_value)
                        balance -= entry_comm  # deduct entry commission
                        
                        spread_offset = (self.costs.spread_pips * pip_unit)
                        slip_offset = (self.costs.slippage_pips * pip_unit)

                        if signal.action == SignalAction.BUY:
                            executed_px = bar["close"] + spread_offset + slip_offset
                        else:
                            executed_px = bar["close"] - slip_offset

                        open_position = {
                            "action": signal.action,
                            "entry_price": executed_px,
                            "sl": signal.stop_loss,
                            "tp": signal.take_profit,
                            "lot": calculated_lot,
                            "entry_time": idx,
                            "entry_comm": entry_comm
                        }

        # Rekap hasil
        winning_trades = [t for t in trades if t.pnl > 0]
        losing_trades = [t for t in trades if t.pnl <= 0]
        win_rate = (len(winning_trades) / len(trades) * 100.0) if trades else 0.0

        total_gain = sum(t.pnl for t in winning_trades)
        total_loss = abs(sum(t.pnl for t in losing_trades))
        profit_factor = (total_gain / total_loss) if total_loss > 0 else (999.0 if total_gain > 0 else 0.0)

        return BacktestResult(
            initial_balance=self.initial_balance,
            final_balance=balance,
            net_profit=balance - self.initial_balance,
            total_trades=len(trades),
            winning_trades=len(winning_trades),
            losing_trades=len(losing_trades),
            win_rate=win_rate,
            profit_factor=profit_factor,
            max_daily_drawdown_pct=max_daily_dd,
            max_total_drawdown_pct=max_total_dd,
            passed_prop_rules=passed_rules,
            breach_reason=breach_reason,
            trades=trades,
            equity_curve=equity_curve
        )
