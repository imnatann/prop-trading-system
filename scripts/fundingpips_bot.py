"""
FundingPips Automated Trading Bot Daemon.

Menjalankan strategi trading kuantitatif secara otomatis penuh:
  1. Terhubung ke MetaTrader 5 (riil di Windows atau --mock di macOS/Linux)
  2. Memantau aturan ketat FundingPips (Batas Daily Drawdown 5%, Max Loss 10%, Anti-Scalping 60s)
  3. Mengambil data bar candle secara berkala (default M15)
  4. Menganalisis kondisi pasar dengan strategi kuantitatif (EURUSD Multi-Timeframe Trend & Momentum)
  5. Menghitung ukuran lot aman secara dinamis berdasarkan Stop Loss (PositionSizer 0.5% risk)
  6. Mengirim order secara mandiri saat sinyal BUY/SELL terkonfirmasi
  7. Mencatat seluruh riwayat eksekusi ke data/trades/trades.csv dan trades.jsonl

Penggunaan:
    # Mode Monitor / Dry Run (menganalisis pasar, mencetak sinyal, tanpa mengirim order)
    python -m scripts.fundingpips_bot --symbol EURUSD

    # Mode Eksekusi Otomatis Penuh (Live Trading)
    python -m scripts.fundingpips_bot --symbol EURUSD --allow-order

    # Mode Simulasi di Mac/Linux (--mock)
    python -m scripts.fundingpips_bot --symbol EURUSD --mock --allow-order
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config.fundingpips_rules import TWO_STEP_STANDARD
from scripts.fundingpips_cli import banner, build_adapter, connect_and_report, field, safe_main
from src.execution.errors import OrderPreflightError
from src.execution.models import OrderIntent, OrderSide
from src.execution.telemetry import attribute_round_trip, exec_event
from src.execution.trade_journal import TradeJournal, get_trade_journal
from src.broker.models import SymbolSpec
from src.risk.position_sizer import PositionSizer
from src.strategy.base import SignalAction, TradeSignal
from src.strategy.trend_v1 import EURUSDMultiTimeframeTrendStrategy


def run(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="FundingPips Automated Trading Bot Daemon.")
    parser.add_argument("--symbol", default="EURUSD", help="Trading symbol (e.g. EURUSD, BTCUSD)")
    parser.add_argument("--timeframe", default="M15", help="Bar timeframe for strategy (default M15)")
    parser.add_argument("--risk-pct", type=float, default=0.5,
                        help="Risk per trade in percent of equity (default 0.5%%)")
    parser.add_argument("--poll-interval", type=float, default=10.0,
                        help="Seconds between market scans (default 10s)")
    parser.add_argument("--max-spread-pips", type=float, default=None,
                        help="Maximum allowable spread in pips (defaults to 3.0 for Forex, 5000.0 for Crypto)")
    parser.add_argument("--max-trades-day", type=int, default=3,
                        help="Maximum trades permitted per calendar day (default 3)")
    parser.add_argument("--allow-order", action="store_true",
                        help="Enable LIVE order placement (without this flag, bot runs in DRY RUN mode)")
    parser.add_argument("--mock", action="store_true",
                        help="Run with simulated MT5 gateway (for testing on macOS/Linux)")
    parser.add_argument("--max-iterations", type=int, default=0,
                        help="Exit after N iterations (0 for infinite loop; useful for tests)")
    parser.add_argument("--trades-dir", default=None,
                        help="Directory to save trades ledger (defaults to data/trades/)")
    args = parser.parse_args(argv)

    is_crypto = "BTC" in args.symbol.upper() or "CRYPTO" in args.symbol.upper()
    default_ceiling = 5000.0 if is_crypto else 3.0
    effective_max_spread = args.max_spread_pips if args.max_spread_pips is not None else default_ceiling

    # 1. Build adapter
    can_place = bool(args.allow_order)
    adapter = build_adapter(
        allow_order=can_place,
        max_spread_pips=effective_max_spread,
        mock=args.mock,
        symbol=args.symbol,
    )
    cfg = adapter.config

    failure = connect_and_report(adapter)
    if failure is not None:
        return failure

    journal = TradeJournal(Path(args.trades_dir) if args.trades_dir else None)
    sizer = PositionSizer()
    strategy = EURUSDMultiTimeframeTrendStrategy()

    banner("FundingPips Automated Trading Bot")
    field("Symbol", args.symbol)
    field("Strategy", strategy.name)
    field("Timeframe", args.timeframe)
    field("Risk per trade", "%.2f%%" % args.risk_pct)
    field("Max trades / day", args.max_trades_day)
    field("Execution mode", "LIVE AUTONOMOUS TRADING" if can_place and cfg.allow_trading else "PAPER MONITOR (DRY RUN)")
    field("Trade Journal", journal.directory)
    print()

    # Track session state
    initial_account = adapter.account_info()
    initial_balance = float(initial_account.balance)
    daily_baseline = float(max(initial_account.balance, initial_account.equity))

    # Daily floor (5%) & Max total floor (10%)
    daily_floor = daily_baseline * (1.0 - (TWO_STEP_STANDARD.daily_loss_pct / 100.0))
    total_floor = initial_balance * (1.0 - (TWO_STEP_STANDARD.max_loss_pct / 100.0))

    field("Account initial balance", "$%.2f" % initial_balance)
    field("Daily Loss Floor (5%)", "$%.2f" % daily_floor)
    field("Max Loss Floor (10%)", "$%.2f" % total_floor)
    print()
    print("Bot loop started. Press Ctrl+C to stop.")
    print("=" * 68)

    trades_today = 0
    iteration = 0
    last_position_ticket: Optional[str] = None
    open_entry_time: float = 0.0

    try:
        while True:
            iteration += 1
            now_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

            # Check broker connection & account metrics
            account = adapter.account_info()
            equity = float(account.equity)
            balance = float(account.balance)

            # --- HARD RISK GUARD (FundingPips Rules) ---
            if equity <= daily_floor or balance <= daily_floor:
                banner("EMERGENCY RISK BREACH: DAILY LOSS FLOOR REACHED")
                print("Equity: $%.2f | Daily Floor: $%.2f" % (equity, daily_floor))
                print("Halting all automated trading immediately to protect account!")
                # Close any remaining position
                for p in adapter.positions(args.symbol):
                    adapter.close_position(p.position_id, reason="DAILY_FLOOR_PROTECT")
                return 5

            if equity <= total_floor or balance <= total_floor:
                banner("EMERGENCY RISK BREACH: TOTAL LOSS FLOOR REACHED")
                print("Equity: $%.2f | Max Total Floor: $%.2f" % (equity, total_floor))
                return 5

            # --- POSITION MONITORING ---
            open_positions = adapter.positions(args.symbol)
            has_open_position = len(open_positions) > 0

            # Tick info
            tick = adapter.tick(args.symbol)
            sym_info = adapter.symbol_info(args.symbol)
            spread_pips = (tick.ask - tick.bid) / sym_info.pip_size if sym_info.pip_size else 0.0

            if has_open_position:
                pos = open_positions[0]
                last_position_ticket = pos.position_id
                elapsed = time.time() - open_entry_time if open_entry_time > 0 else 60.0
                print("[%s] POS OPEN: %s %.2f lot @ %.5f | Cur: %.5f | Profit: $%.2f | Open: %.0fs" % (
                    now_str, pos.side, pos.volume, pos.entry_price, tick.bid if pos.side == "BUY" else tick.ask,
                    pos.profit, elapsed
                ))
            else:
                # If a position just closed externally (SL/TP hit in MT5), log close attribution
                if last_position_ticket is not None:
                    print("[%s] Position %s was CLOSED by MT5 (SL/TP target hit)." % (now_str, last_position_ticket))
                    last_position_ticket = None
                    open_entry_time = 0.0

                # --- SIGNAL GENERATION ---
                bars = adapter.get_bars(args.symbol, timeframe=args.timeframe, count=100)
                signal: TradeSignal = strategy.generate_signal(args.symbol, bars) if len(bars) >= 30 else TradeSignal(
                    symbol=args.symbol, action=SignalAction.HOLD, entry_price=0.0, stop_loss=0.0, take_profit=0.0, rationale="Collecting bars"
                )

                status_line = "[%s] Balance: $%.2f | Eq: $%.2f | Spread: %.1fp | Signal: %s (%s)" % (
                    now_str, balance, equity, spread_pips, signal.action.value, signal.rationale
                )
                print(status_line)

                # --- EXECUTION TRIGGER ---
                if signal.action in (SignalAction.BUY, SignalAction.SELL):
                    if trades_today >= args.max_trades_day:
                        print("  [!] Max daily trades limit reached (%d/%d). Skipping signal." % (trades_today, args.max_trades_day))
                    elif spread_pips > effective_max_spread:
                        print("  [!] Spread %.2f pips exceeds ceiling %.2f pips. Skipping signal." % (spread_pips, effective_max_spread))
                    else:
                        # Dynamic Risk Lot Sizing using Broker-Native Spec
                        spec = SymbolSpec(
                            symbol=args.symbol,
                            tick_size=sym_info.trade_tick_size or sym_info.point or 0.01,
                            tick_value=sym_info.trade_tick_value or (0.01 if is_crypto else 1.0),
                            contract_size=sym_info.contract_size or 1.0,
                            volume_min=sym_info.volume_min or 0.01,
                            volume_max=sym_info.volume_max or 1.0,
                            volume_step=sym_info.volume_step or 0.01,
                            digits=sym_info.digits,
                        )
                        raw_lot = sizer.calculate_lot(
                            equity=equity,
                            risk_pct=args.risk_pct,
                            entry_price=signal.entry_price or (tick.ask if signal.action == SignalAction.BUY else tick.bid),
                            stop_loss=signal.stop_loss,
                            symbol=args.symbol,
                            spec=spec,
                            allow_sub_min_rounding=True,
                        )
                        # Clamp lot to broker boundaries
                        lot = max(sym_info.volume_min, min(round(raw_lot, 2), sym_info.volume_max))
                        side_enum = OrderSide.BUY if signal.action == SignalAction.BUY else OrderSide.SELL

                        price_fmt = "%.2f" if is_crypto else "%.5f"
                        print(("  => NEW SIGNAL DETECTED: %s %.2f lot | SL: " + price_fmt + " | TP: " + price_fmt) % (
                            signal.action.value, lot, signal.stop_loss, signal.take_profit
                        ))

                        if can_place and cfg.allow_trading:
                            intent = OrderIntent(
                                symbol=args.symbol,
                                side=side_enum,
                                volume=lot,
                                sl=signal.stop_loss,
                                tp=signal.take_profit,
                                comment="QP-AUTO",
                            )
                            res = adapter.place_order(intent)
                            if res.success:
                                trades_today += 1
                                open_entry_time = time.time()
                                last_position_ticket = res.order_id
                                print("  [SUCCESS] Order FILLED! Ticket: %s @ %.5f" % (res.order_id, res.filled_price))
                                journal.record_open(
                                    trade_id=str(res.order_id),
                                    symbol=args.symbol,
                                    side=signal.action.value,
                                    volume=lot,
                                    requested_price=res.requested_price,
                                    filled_price=res.filled_price,
                                    slippage_pips=res.slippage_pips(sym_info.pip_size),
                                    sl=signal.stop_loss,
                                    tp=signal.take_profit,
                                    mode="MOCK" if args.mock else "LIVE",
                                    comment="QP-AUTO-SIGNAL",
                                    account=account.masked_login,
                                    server=account.server,
                                )
                            else:
                                print("  [REJECTED] Order failed: %s (%s)" % (res.retcode_name, res.error_message))
                        else:
                            print("  [DRY RUN] Order NOT submitted. Run with --allow-order to execute automatically.")

            if args.max_iterations > 0 and iteration >= args.max_iterations:
                print("Max iterations (%d) reached. Exiting cleanly." % args.max_iterations)
                break

            time.sleep(args.poll_interval)

    except KeyboardInterrupt:
        print()
        print("Bot loop stopped by operator.")
    finally:
        adapter.disconnect()
        print("Session complete. Adapter disconnected cleanly.")

    return 0


if __name__ == "__main__":
    sys.exit(safe_main(run))
