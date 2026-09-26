"""Event-driven backtest simulator v2 - accounting that cannot lie.

Design contract (enforced by tests in tests/unit/test_simulator_v2_invariants.py):

  mid price -> bid/ask construction -> order side -> execution slippage
  -> position accounting -> commissions -> financing -> realized/unrealized P&L

Invariants:

  Equity_t     = Balance_t + UnrealizedPnL_t
  FinalEquity  = FinalBalance                  after explicit liquidation
  DeltaBalance = sum(NetPnL_i)                 once every position is closed
  NetPnL_i     = Gross - Spread - Slippage - Commission - Financing

Key modelling decisions and why:

* Spread is realised through the BID/ASK execution prices, never debited again.
  spread_cost is attribution only. Double-charging the spread was the single
  largest accounting error in the old engine.
* Slippage is charged EXACTLY ONCE per side, inside the execution price. The old
  engine charged it in the price AND inside compute_entry/exit_costs (2x).
* BUY entry = ask + slip, BUY exit = bid - slip;
  SELL entry = bid - slip, SELL exit = ask + slip.
  Both sides therefore pay the spread symmetrically. The old engine applied the
  spread only to BUY, which biased every backtest in favour of SELL by ~1 pip/trade.
* Financing is applied per night held, via an injectable rollover_multiplier
  callable. Triple-swap Wednesday is DATA, not engine logic.
* Holding through the weekend is ALLOWED by the engine. Whether to flatten before
  Friday is a strategy/risk policy (force_close_friday), not an accounting truth.
* Any position still open at the end of the sample is explicitly liquidated and its
  full breakdown recorded, so no commission is charged without its matching P&L.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, List, Optional

import numpy as np
import pandas as pd

from src.strategy.base import BaseStrategy, SignalAction, TradeSignal

PIP = 0.0001


@dataclass
class InstrumentSpec:
    symbol: str = "EURUSD"
    pip: float = PIP
    digits: int = 5
    contract_size: float = 100_000.0
    pip_value_per_lot: float = 10.0
    volume_min: float = 0.01
    volume_max: float = 50.0
    volume_step: float = 0.01


@dataclass
class CostSpec:
    """Execution costs. Slippage lives ONLY here - charged once, in the price."""
    spread_pips: float = 1.0
    slippage_pips: float = 0.2
    commission_per_lot_per_side: float = 3.0
    swap_long_pips: float = -0.5
    swap_short_pips: float = 0.1


@dataclass
class RiskSpec:
    risk_per_trade_pct: float = 0.5
    max_leverage: float = 30.0


def default_rollover_multiplier(ts) -> float:
    """1.0 normally, 3.0 on Wednesday. Injectable, because brokers differ."""
    return 3.0 if pd.Timestamp(ts).dayofweek == 2 else 1.0


@dataclass
class TradeV2:
    trade_id: int
    symbol: str
    side: str
    entry_time: Any
    exit_time: Any
    entry_price: float
    exit_price: float
    entry_mid: float
    exit_mid: float
    lot_size: float
    bars_held: int
    nights_held: float
    gross_pnl: float
    spread_cost: float
    slippage_cost: float
    commission: float
    financing: float
    net_pnl: float
    exit_reason: str

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class BacktestResultV2:
    symbol: str
    initial_balance: float
    final_balance: float
    final_equity: float
    net_profit: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    profit_factor: float
    gross_profit: float
    gross_loss: float
    total_spread_cost: float
    total_slippage_cost: float
    total_commission: float
    total_financing: float
    max_equity_drawdown_pct: float
    trades: List[TradeV2] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)
    balance_curve: List[float] = field(default_factory=list)
    timestamps: List[Any] = field(default_factory=list)

    def sum_net_pnl(self) -> float:
        return float(sum(t.net_pnl for t in self.trades))

    def balance_delta(self) -> float:
        return float(self.final_balance - self.initial_balance)

    def cost_components_per_trade(self) -> List[float]:
        return [t.gross_pnl - t.spread_cost - t.slippage_cost - t.commission
                - t.financing for t in self.trades]


class PropBacktestSimulatorV2:
    """Bar-by-bar simulator with auditable accounting.

    Bar semantics: the signal is computed on data up to and including bar i and the
    fill happens at bar i+1 OPEN. Using the same bar close for both signal and fill
    is a subtle look-ahead and is avoided here.
    """

    def __init__(self, spec=None, costs=None, risk=None, initial_balance=100000.0,
                 rollover_multiplier=default_rollover_multiplier,
                 force_close_friday=False, friday_close_hour_utc=20):
        self.spec = spec or InstrumentSpec()
        self.costs = costs or CostSpec()
        self.risk = risk or RiskSpec()
        self.initial_balance = initial_balance
        self.rollover_multiplier = rollover_multiplier
        self.force_close_friday = force_close_friday
        self.friday_close_hour_utc = friday_close_hour_utc

    # ---- price construction ----
    def _half_spread(self) -> float:
        return self.costs.spread_pips * self.spec.pip / 2.0

    def ask(self, mid: float) -> float:
        return mid + self._half_spread()

    def bid(self, mid: float) -> float:
        return mid - self._half_spread()

    def _slip(self) -> float:
        return self.costs.slippage_pips * self.spec.pip

    def entry_price(self, action, mid: float) -> float:
        """BUY lifts the ask, SELL hits the bid. Slippage always adverse."""
        if action == SignalAction.BUY:
            return self.ask(mid) + self._slip()
        return self.bid(mid) - self._slip()

    def exit_price(self, action, mid: float) -> float:
        """Closing a BUY sells at the bid; closing a SELL buys at the ask."""
        if action == SignalAction.BUY:
            return self.bid(mid) - self._slip()
        return self.ask(mid) + self._slip()

    def _pips(self, action, entry: float, exit_: float) -> float:
        if action == SignalAction.BUY:
            return (exit_ - entry) / self.spec.pip
        return (entry - exit_) / self.spec.pip

    def _cash_per_pip(self, lot: float) -> float:
        return self.spec.pip_value_per_lot * lot

    def _position_size(self, balance: float, entry: float, stop: float) -> float:
        stop_dist = abs(entry - stop) / self.spec.pip
        if stop_dist <= 0:
            return 0.0
        risk_cash = balance * self.risk.risk_per_trade_pct / 100.0
        lot = risk_cash / (stop_dist * self.spec.pip_value_per_lot)
        step = self.spec.volume_step
        lot = np.floor(lot / step) * step
        lots_cap = self.spec.volume_max
        if self.risk.max_leverage > 0:
            notional_cap = balance * self.risk.max_leverage
            lots_cap = min(lots_cap, notional_cap / self.spec.contract_size)
        return float(max(0.0, min(lot, lots_cap)))

    # ---- main loop ----
    def run(self, strategy, df: pd.DataFrame, symbol=None) -> BacktestResultV2:
        symbol = symbol or self.spec.symbol
        if not {"open", "high", "low", "close"}.issubset(df.columns):
            raise ValueError("df must have open/high/low/close columns")

        balance = float(self.initial_balance)
        pos = None
        trades = []
        equity_curve = [balance]
        balance_curve = [balance]
        stamps = [df.index[0]]
        trade_id = 1
        last_mid = float(df["close"].iloc[0])

        for i in range(len(df)):
            ts = df.index[i]
            bar = df.iloc[i]
            mid_close = float(bar["close"])

            # ---- 1. manage an open position on THIS bar ----
            if pos is not None:
                pos["bars_held"] += 1
                action = pos["action"]
                hit = None
                if action == SignalAction.BUY:
                    if float(bar["low"]) <= pos["sl"]:
                        hit = "SL"
                    elif float(bar["high"]) >= pos["tp"]:
                        hit = "TP"
                else:
                    if float(bar["high"]) >= pos["sl"]:
                        hit = "SL"
                    elif float(bar["low"]) <= pos["tp"]:
                        hit = "TP"
                if pos["bars_held"] >= pos["time_stop"] and hit is None:
                    hit = "TIME"
                if (self.force_close_friday and pd.Timestamp(ts).dayofweek == 4
                        and pd.Timestamp(ts).hour >= self.friday_close_hour_utc
                        and hit is None):
                    hit = "FRIDAY"

                if hit:
                    if hit == "SL":
                        ref = pos["sl"]
                    elif hit == "TP":
                        ref = pos["tp"]
                    else:
                        ref = mid_close
                    balance, tr = self._close(balance, pos, ref, ts, hit, trade_id)
                    trades.append(tr)
                    trade_id += 1
                    pos = None

            # ---- 2. financing for holding overnight ----
            if pos is not None and i > 0:
                prev_ts = df.index[i - 1]
                if pd.Timestamp(ts).normalize() > pd.Timestamp(prev_ts).normalize():
                    mult = float(self.rollover_multiplier(ts))
                    cpp = self._cash_per_pip(pos["lot"])
                    rate = (self.costs.swap_long_pips
                            if pos["action"] == SignalAction.BUY
                            else self.costs.swap_short_pips)
                    charge = rate * cpp * mult
                    balance += charge
                    pos["financing"] += charge
                    pos["nights"] += mult

            # ---- 3. mark to market ----
            if pos is not None:
                unreal = (self._pips(pos["action"], pos["entry_price"], mid_close)
                          * self._cash_per_pip(pos["lot"]))
                equity = balance + unreal
            else:
                equity = balance
            equity_curve.append(equity)
            balance_curve.append(balance)
            stamps.append(ts)
            last_mid = mid_close

            # ---- 4. new entry, filled at NEXT bar open ----
            if pos is None and i >= 21 and (i + 1) < len(df):
                sig = strategy.generate_signal(symbol, df.iloc[: i + 1])
                if sig.action in (SignalAction.BUY, SignalAction.SELL):
                    fill_mid = float(df.iloc[i + 1]["open"])
                    entry = self.entry_price(sig.action, fill_mid)
                    lot = self._position_size(balance, entry, float(sig.stop_loss))
                    if lot >= self.spec.volume_min:
                        comm = self.costs.commission_per_lot_per_side * lot
                        balance -= comm
                        pos = {
                            "action": sig.action,
                            "entry_price": entry,
                            "entry_mid": fill_mid,
                            "sl": float(sig.stop_loss),
                            "tp": float(sig.take_profit),
                            "lot": lot,
                            "entry_time": df.index[i + 1],
                            "bars_held": 0,
                            "time_stop": int(getattr(sig, "time_stop", 0) or 10 ** 9),
                            "commission": comm,
                            "financing": 0.0,
                            "nights": 0.0,
                        }

        # ---- 5. explicit liquidation of anything still open ----
        if pos is not None:
            balance, tr = self._close(balance, pos, last_mid, df.index[-1],
                                      "FINAL_LIQUIDATION", trade_id)
            trades.append(tr)
            pos = None
            equity_curve.append(balance)
            balance_curve.append(balance)
            stamps.append(df.index[-1])

        return self._summarise(symbol, balance, trades, equity_curve,
                               balance_curve, stamps)

    def _close(self, balance: float, pos, ref_mid: float, ts, reason: str,
               trade_id: int):
        action = pos["action"]
        lot = pos["lot"]
        exit_px = self.exit_price(action, ref_mid)

        gross = (self._pips(action, pos["entry_price"], exit_px)
                 * self._cash_per_pip(lot))

        # attribution only - the spread is already inside entry/exit prices
        spread_attr = abs(
            (self._pips(action, pos["entry_mid"], pos["entry_price"])
             + self._pips(action, exit_px, ref_mid)) * self._cash_per_pip(lot))
        slip_attr = 2.0 * self._slip() / self.spec.pip * self._cash_per_pip(lot)

        exit_comm = self.costs.commission_per_lot_per_side * lot
        balance -= exit_comm
        commission_total = pos["commission"] + exit_comm

        # entry commission and financing were already debited; only gross remains
        balance += gross

        net_accounting = gross - commission_total + pos["financing"]

        tr = TradeV2(
            trade_id=trade_id, symbol=self.spec.symbol, side=action.value,
            entry_time=pos["entry_time"], exit_time=ts,
            entry_price=pos["entry_price"], exit_price=exit_px,
            entry_mid=pos["entry_mid"], exit_mid=ref_mid,
            lot_size=lot, bars_held=pos["bars_held"],
            nights_held=pos["nights"], gross_pnl=gross,
            spread_cost=spread_attr, slippage_cost=slip_attr,
            commission=commission_total, financing=pos["financing"],
            net_pnl=net_accounting, exit_reason=reason,
        )
        return balance, tr

    def _summarise(self, symbol, final_balance, trades, equity_curve,
                   balance_curve, stamps) -> BacktestResultV2:
        wins = [t for t in trades if t.net_pnl > 0]
        losses = [t for t in trades if t.net_pnl <= 0]
        gp = float(sum(t.net_pnl for t in wins))
        gl = float(abs(sum(t.net_pnl for t in losses)))
        eq = np.asarray(equity_curve, dtype=float)
        peak = np.maximum.accumulate(eq)
        dd = np.where(peak > 0, (peak - eq) / peak * 100.0, 0.0)
        return BacktestResultV2(
            symbol=symbol, initial_balance=self.initial_balance,
            final_balance=float(final_balance), final_equity=float(final_balance),
            net_profit=float(final_balance - self.initial_balance),
            total_trades=len(trades), winning_trades=len(wins),
            losing_trades=len(losses),
            win_rate=(len(wins) / len(trades) * 100.0) if trades else 0.0,
            profit_factor=(gp / gl) if gl > 0 else (999.0 if gp > 0 else 0.0),
            gross_profit=gp, gross_loss=gl,
            total_spread_cost=float(sum(t.spread_cost for t in trades)),
            total_slippage_cost=float(sum(t.slippage_cost for t in trades)),
            total_commission=float(sum(t.commission for t in trades)),
            total_financing=float(sum(t.financing for t in trades)),
            max_equity_drawdown_pct=float(dd.max()) if len(dd) else 0.0,
            trades=trades, equity_curve=list(eq),
            balance_curve=list(balance_curve), timestamps=list(stamps),
        )
