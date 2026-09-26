"""
Broker Adapter & Abstraction Layer.
Menyediakan antarmuka seragam ke broker:
- MockBrokerAdapter: untuk testing & development di macOS tanpa dependensi MT5.
- MT5BrokerAdapter: untuk eksekusi nyata di Windows VPS dengan MetaTrader 5 native.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from loguru import logger

from src.broker.base import BaseBrokerAdapter, OrderResult, PositionInfo
from src.broker.models import SymbolSpec
from src.risk.drawdown_monitor import AccountSnapshot
from src.strategy.base import SignalAction

# Alias BrokerAdapter ke BaseBrokerAdapter untuk backward-compatibility
BrokerAdapter = BaseBrokerAdapter


class MockBrokerAdapter(BaseBrokerAdapter):
    """
    Mock broker adapter untuk lingkungan development di macOS.
    Menyimulasikan saldo akun, pergerakan spread, dan eksekusi order secara lokal.
    """

    def __init__(self, initial_balance: float = 100_000.0):
        self.balance = initial_balance
        self.equity = initial_balance
        self.positions: List[PositionInfo] = []
        self._executed_orders: Dict[str, OrderResult] = {}
        self._connected = False
        self._last_heartbeat = datetime.now(timezone.utc)
        self._mock_prices = {
            "EURUSD": {"bid": 1.08500, "ask": 1.08512, "spread_pips": 1.2},
            "GBPUSD": {"bid": 1.26500, "ask": 1.26515, "spread_pips": 1.5},
            "USDJPY": {"bid": 154.200, "ask": 154.214, "spread_pips": 1.4},
            "XAUUSD": {"bid": 2650.50, "ask": 2650.75, "spread_pips": 2.5},
        }

    def connect(self) -> bool:
        self._connected = True
        self._last_heartbeat = datetime.now(timezone.utc)
        logger.info(f"MockBrokerAdapter connected. Initial Balance: ${self.balance:,.2f}")
        return True

    def disconnect(self) -> None:
        self._connected = False
        logger.info("MockBrokerAdapter disconnected.")

    def health(self) -> Dict[str, Any]:
        return {
            "status": "HEALTHY" if self._connected else "DISCONNECTED",
            "connected": self._connected,
            "latency_ms": 1.2,
            "last_heartbeat": self._last_heartbeat.isoformat()
        }

    def get_server_time(self) -> datetime:
        return datetime.now(timezone.utc)

    def set_mock_equity(self, equity: float) -> None:
        """Memanipulasi nilai equity untuk memverifikasi kalkulasi drawdown."""
        self.equity = equity

    def set_mock_spread(self, symbol: str, spread_pips: float) -> None:
        """Memanipulasi spread untuk menguji filter spread."""
        if symbol in self._mock_prices:
            self._mock_prices[symbol]["spread_pips"] = spread_pips

    def get_account_snapshot(self) -> AccountSnapshot:
        margin_used = len(self.positions) * 1000.0
        return AccountSnapshot(
            balance=self.balance,
            equity=self.equity,
            margin=margin_used,
            free_margin=self.equity - margin_used
        )

    def get_symbol_spec(self, symbol: str) -> SymbolSpec:
        sym = symbol.upper()
        if "JPY" in sym:
            return SymbolSpec(
                symbol=sym,
                tick_size=0.001,
                tick_value=0.65,
                contract_size=100000.0,
                volume_min=0.01,
                volume_max=50.0,
                volume_step=0.01,
                digits=3,
                spread_limit_pips=3.0
            )
        elif "XAU" in sym or "GOLD" in sym:
            return SymbolSpec(
                symbol=sym,
                tick_size=0.01,
                tick_value=1.0,
                contract_size=100.0,
                volume_min=0.01,
                volume_max=50.0,
                volume_step=0.01,
                digits=2,
                spread_limit_pips=5.0
            )
        else:
            return SymbolSpec(
                symbol=sym,
                tick_size=0.00001,
                tick_value=1.0,
                contract_size=100000.0,
                volume_min=0.01,
                volume_max=50.0,
                volume_step=0.01,
                digits=5,
                spread_limit_pips=3.0
            )

    def get_symbol_price(self, symbol: str) -> Tuple[float, float, float]:
        sym = symbol.upper()
        if sym in self._mock_prices:
            p = self._mock_prices[sym]
            return p["bid"], p["ask"], p["spread_pips"]
        return 1.00000, 1.00010, 1.0

    def get_positions(self, symbol: Optional[str] = None) -> List[PositionInfo]:
        if symbol is None:
            return list(self.positions)
        return [p for p in self.positions if p.symbol == symbol.upper()]

    def get_open_positions_count(self, symbol: Optional[str] = None) -> int:
        if symbol is None:
            return len(self.positions)
        return sum(1 for p in self.positions if p.symbol == symbol.upper())

    def execute_order(
        self,
        symbol: str,
        action: SignalAction,
        lot_size: float,
        sl: float,
        tp: float,
        client_order_id: Optional[str] = None,
        comment: str = "PropBot Auto"
    ) -> OrderResult:
        bid, ask, _ = self.get_symbol_price(symbol)
        fill_price = ask if action == SignalAction.BUY else bid

        order_id = f"MOCK-{len(self.positions) + 1:04d}"
        pos = PositionInfo(
            position_id=order_id,
            client_order_id=client_order_id,
            symbol=symbol.upper(),
            action=action.value,
            volume=lot_size,
            entry_price=fill_price,
            sl=sl,
            tp=tp,
            current_price=fill_price,
            profit=0.0,
            open_time=datetime.now(timezone.utc)
        )
        self.positions.append(pos)
        
        result = OrderResult(
            success=True,
            order_id=order_id,
            client_order_id=client_order_id,
            symbol=symbol,
            action=action.value,
            lot_size=lot_size,
            price=fill_price,
            sl=sl,
            tp=tp,
            status="FILLED",
            timestamp_utc=datetime.now(timezone.utc)
        )
        if client_order_id:
            self._executed_orders[client_order_id] = result
        self._executed_orders[order_id] = result

        logger.info(f"MockBroker: Executed {action.value} {lot_size} lot {symbol} @ {fill_price} [SL: {sl}, TP: {tp}]")
        return result

    def get_order(
        self,
        client_order_id: Optional[str] = None,
        broker_order_id: Optional[str] = None
    ) -> Optional[OrderResult]:
        if client_order_id and client_order_id in self._executed_orders:
            return self._executed_orders[client_order_id]
        if broker_order_id and broker_order_id in self._executed_orders:
            return self._executed_orders[broker_order_id]
        return None

    def cancel_order(self, order_id: str) -> bool:
        # Untuk pending order (jika ada)
        return True

    def close_position(self, position_id: str) -> bool:
        initial_len = len(self.positions)
        self.positions = [p for p in self.positions if p.position_id != position_id]
        return len(self.positions) < initial_len

    def close_all_positions(self) -> int:
        count = len(self.positions)
        self.positions.clear()
        logger.warning(f"MockBroker: Emergency Liquidation - closed {count} positions.")
        return count


class MT5BrokerAdapter(BaseBrokerAdapter):
    """
    Adapter MetaTrader 5 generik (Windows VPS production).

    DEPRECATED untuk target FundingPips: gunakan FundingPipsMT5Adapter
    (src/execution/fundingpips_mt5.py) melalui EXECUTION_PROVIDER=fundingpips_mt5.
    Kelas ini TETAP dipertahankan agar jalur eksekusi lama dan seluruh test yang sudah
    lulus tidak rusak; ia tidak dihapus dan perilakunya tidak diubah.

    Catatan keamanan: kelas ini menyimpan password sebagai atribut biasa, sehingga
    repr()-nya berpotensi membocorkan kredensial, dan get_symbol_price() masih
    mengasumsikan pip size secara hardcode. Kedua hal itu justru alasan adapter baru
    dibuat. Jangan gunakan kelas ini untuk kredensial FundingPips yang sebenarnya.
    """

    def __init__(self, login: int, password: str, server: str, path: Optional[str] = None):
        self.login = login
        self.password = password
        self.server = server
        self.path = path
        self._mt5 = None

    def connect(self) -> bool:
        try:
            import MetaTrader5 as mt5
            self._mt5 = mt5
        except ImportError:
            logger.error("Library MetaTrader5 tidak terpasang atau tidak didukung di OS ini (hanya Windows).")
            return False

        init_kwargs = {}
        if self.path:
            init_kwargs["path"] = self.path

        if not self._mt5.initialize(**init_kwargs):
            logger.error(f"MT5 initialize failed: {self._mt5.last_error()}")
            return False

        authorized = self._mt5.login(self.login, password=self.password, server=self.server)
        if not authorized:
            logger.error(f"MT5 login failed: {self._mt5.last_error()}")
            self._mt5.shutdown()
            return False

        logger.info(f"MT5 Connected successfully to account #{self.login} on {self.server}")
        return True

    def disconnect(self) -> None:
        if self._mt5:
            self._mt5.shutdown()
            logger.info("MT5 Disconnected.")

    def health(self) -> Dict[str, Any]:
        if not self._mt5:
            return {"status": "DISCONNECTED", "connected": False, "latency_ms": -1}
        term_info = self._mt5.terminal_info()
        return {
            "status": "HEALTHY" if term_info and term_info.connected else "DEGRADED",
            "connected": bool(term_info and term_info.connected),
            "ping_last": term_info.ping_last if term_info else -1
        }

    def get_server_time(self) -> datetime:
        if not self._mt5:
            return datetime.now(timezone.utc)
        term_info = self._mt5.terminal_info()
        # In real MT5, time current can be fetched from symbol tick
        tick = self._mt5.symbol_info_tick("EURUSD")
        if tick and hasattr(tick, 'time'):
            return datetime.fromtimestamp(tick.time, tz=timezone.utc)
        return datetime.now(timezone.utc)

    def get_account_snapshot(self) -> AccountSnapshot:
        if not self._mt5:
            raise RuntimeError("MT5 not connected")
        acc = self._mt5.account_info()
        return AccountSnapshot(
            balance=acc.balance,
            equity=acc.equity,
            margin=acc.margin,
            free_margin=acc.margin_free
        )

    def get_symbol_spec(self, symbol: str) -> SymbolSpec:
        if not self._mt5:
            raise RuntimeError("MT5 not connected")
        info = self._mt5.symbol_info(symbol)
        if not info:
            raise ValueError(f"Symbol {symbol} tidak ditemukan di server MT5")
        return SymbolSpec(
            symbol=info.name,
            tick_size=info.trade_tick_size,
            tick_value=info.trade_tick_value,
            contract_size=info.trade_contract_size,
            volume_min=info.volume_min,
            volume_max=info.volume_max,
            volume_step=info.volume_step,
            digits=info.digits,
            spread_limit_pips=3.0
        )

    def get_symbol_price(self, symbol: str) -> Tuple[float, float, float]:
        if not self._mt5:
            raise RuntimeError("MT5 not connected")
        tick = self._mt5.symbol_info_tick(symbol)
        pip_size = 0.01 if "JPY" in symbol else 0.0001
        spread_pips = (tick.ask - tick.bid) / pip_size
        return tick.bid, tick.ask, spread_pips

    def get_positions(self, symbol: Optional[str] = None) -> List[PositionInfo]:
        if not self._mt5:
            raise RuntimeError("MT5 not connected")
        raw_positions = self._mt5.positions_get(symbol=symbol) if symbol else self._mt5.positions_get()
        if not raw_positions:
            return []

        results = []
        for p in raw_positions:
            results.append(PositionInfo(
                position_id=str(p.ticket),
                client_order_id=p.comment if p.comment and p.comment.startswith("QP-") else None,
                symbol=p.symbol,
                action="BUY" if p.type == self._mt5.ORDER_TYPE_BUY else "SELL",
                volume=p.volume,
                entry_price=p.price_open,
                sl=p.sl,
                tp=p.tp,
                current_price=p.price_current,
                profit=p.profit,
                swap=p.swap,
                magic=p.magic,
                open_time=datetime.fromtimestamp(p.time, tz=timezone.utc)
            ))
        return results

    def get_open_positions_count(self, symbol: Optional[str] = None) -> int:
        if not self._mt5:
            raise RuntimeError("MT5 not connected")
        positions = self._mt5.positions_get(symbol=symbol) if symbol else self._mt5.positions_get()
        return len(positions) if positions else 0

    def execute_order(
        self,
        symbol: str,
        action: SignalAction,
        lot_size: float,
        sl: float,
        tp: float,
        client_order_id: Optional[str] = None,
        comment: str = "PropBot Auto"
    ) -> OrderResult:
        if not self._mt5:
            raise RuntimeError("MT5 not connected")

        order_type = self._mt5.ORDER_TYPE_BUY if action == SignalAction.BUY else self._mt5.ORDER_TYPE_SELL
        tick = self._mt5.symbol_info_tick(symbol)
        price = tick.ask if action == SignalAction.BUY else tick.bid

        order_comment = client_order_id if client_order_id else comment

        request = {
            "action": self._mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": lot_size,
            "type": order_type,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": 10,
            "magic": 888999,
            "comment": order_comment[:31],  # MT5 comment limit 31 chars
            "type_time": self._mt5.ORDER_TIME_GTC,
            "type_filling": self._mt5.ORDER_FILLING_IOC,
        }

        result = self._mt5.order_send(request)
        if result.retcode != self._mt5.TRADE_RETCODE_DONE:
            err = f"MT5 order_send failed: {result.retcode} ({result.comment})"
            logger.error(err)
            return OrderResult(
                success=False,
                order_id=None,
                client_order_id=client_order_id,
                symbol=symbol,
                action=action.value,
                lot_size=lot_size,
                price=price,
                sl=sl,
                tp=tp,
                status="REJECTED",
                error_message=err,
                timestamp_utc=datetime.now(timezone.utc)
            )

        return OrderResult(
            success=True,
            order_id=str(result.order),
            client_order_id=client_order_id,
            symbol=symbol,
            action=action.value,
            lot_size=lot_size,
            price=result.price,
            sl=sl,
            tp=tp,
            status="FILLED",
            timestamp_utc=datetime.now(timezone.utc)
        )

    def get_order(
        self,
        client_order_id: Optional[str] = None,
        broker_order_id: Optional[str] = None
    ) -> Optional[OrderResult]:
        if not self._mt5:
            return None
        if broker_order_id:
            orders = self._mt5.history_orders_get(ticket=int(broker_order_id))
            if orders and len(orders) > 0:
                ord_info = orders[0]
                return OrderResult(
                    success=True,
                    order_id=str(ord_info.ticket),
                    client_order_id=ord_info.comment,
                    symbol=ord_info.symbol,
                    action="BUY" if ord_info.type == self._mt5.ORDER_TYPE_BUY else "SELL",
                    lot_size=ord_info.volume_initial,
                    price=ord_info.price_open,
                    sl=ord_info.sl,
                    tp=ord_info.tp,
                    status="FILLED" if ord_info.state == self._mt5.ORDER_STATE_FILLED else "UNKNOWN",
                    timestamp_utc=datetime.fromtimestamp(ord_info.time_done, tz=timezone.utc)
                )
        return None

    def cancel_order(self, order_id: str) -> bool:
        if not self._mt5:
            return False
        req = {
            "action": self._mt5.TRADE_ACTION_REMOVE,
            "order": int(order_id)
        }
        res = self._mt5.order_send(req)
        return res.retcode == self._mt5.TRADE_RETCODE_DONE

    def close_position(self, position_id: str) -> bool:
        if not self._mt5:
            return False
        positions = self._mt5.positions_get(ticket=int(position_id))
        if not positions:
            return False
        pos = positions[0]
        close_type = self._mt5.ORDER_TYPE_SELL if pos.type == self._mt5.ORDER_TYPE_BUY else self._mt5.ORDER_TYPE_BUY
        tick = self._mt5.symbol_info_tick(pos.symbol)
        price = tick.bid if pos.type == self._mt5.ORDER_TYPE_BUY else tick.ask

        req = {
            "action": self._mt5.TRADE_ACTION_DEAL,
            "position": pos.ticket,
            "symbol": pos.symbol,
            "volume": pos.volume,
            "type": close_type,
            "price": price,
            "deviation": 20,
            "magic": 888999,
            "comment": "Close Position",
            "type_time": self._mt5.ORDER_TIME_GTC,
            "type_filling": self._mt5.ORDER_FILLING_IOC,
        }
        res = self._mt5.order_send(req)
        return res.retcode == self._mt5.TRADE_RETCODE_DONE

    def close_all_positions(self) -> int:
        if not self._mt5:
            return 0
        positions = self._mt5.positions_get()
        if not positions:
            return 0

        closed = 0
        for pos in positions:
            if self.close_position(str(pos.ticket)):
                closed += 1
        return closed
