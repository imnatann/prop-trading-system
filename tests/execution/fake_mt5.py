"""A fake MetaTrader5 gateway.

Mirrors the subset of the official MetaTrader5 API this repository touches, so the
whole execution layer can be tested on macOS/Linux with no terminal, no network, and
no credentials. It also RECORDS every call, which is how tests prove properties like
"an authentication failure is never retried" and "trading disabled sends no order".
"""
from __future__ import annotations
import time

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# --------------------------------------------------------------------- records
@dataclass
class FakeTerminalInfo:
    connected: bool = True
    ping_last: int = 42
    path: str = "C:/MT5/terminal64.exe"
    trade_allowed: bool = True


@dataclass
class FakeAccount:
    login: int = 12345678
    server: str = "FundingPips-Trial"
    currency: str = "USD"
    balance: float = 10_000.0
    equity: float = 10_000.0
    margin: float = 0.0
    margin_free: float = 10_000.0
    margin_level: float = 0.0
    leverage: int = 100
    trade_allowed: bool = True
    trade_expert: bool = True
    company: str = "FundingPips"
    trade_mode: int = 0


@dataclass
class FakeSymbol:
    name: str = "EURUSD"
    digits: int = 5
    point: float = 0.00001
    trade_tick_size: float = 0.00001
    trade_tick_value: float = 1.0
    trade_contract_size: float = 100_000.0
    volume_min: float = 0.01
    volume_max: float = 50.0
    volume_step: float = 0.01
    trade_stops_level: int = 0
    trade_freeze_level: int = 0
    trade_mode: int = 4
    swap_long: float = -8.0
    swap_short: float = 2.0
    swap_mode: int = 1
    swap_rollover3days: int = 3
    currency_base: str = "EUR"
    currency_profit: str = "USD"
    currency_margin: str = "EUR"
    trade_exemode: int = 2
    filling_mode: int = 2  # SYMBOL_FILLING_IOC only
    spread: int = 12
    visible: bool = True


@dataclass
class FakeTick:
    bid: float = 1.08500
    ask: float = 1.08512
    last: float = 1.08506
    volume: float = 10
    time: int = 0
    flags: int = 6

    def __post_init__(self):
        if self.time == 0:
            self.time = int(time.time())


@dataclass
class FakePosition:
    ticket: int = 1001
    symbol: str = "EURUSD"
    type: int = 0
    volume: float = 0.01
    price_open: float = 1.08512
    price_current: float = 1.08520
    sl: float = 0.0
    tp: float = 0.0
    profit: float = 0.8
    swap: float = 0.0
    magic: int = 888999
    comment: str = "QP-SMOKE-1"
    time: int = 1_700_000_000


@dataclass
class FakeOrder:
    ticket: int = 2001
    symbol: str = "EURUSD"
    type: int = 2
    volume_current: float = 0.01
    price_open: float = 1.08000
    sl: float = 0.0
    tp: float = 0.0
    magic: int = 888999
    comment: str = "pending"
    time_setup: int = 1_700_000_000


@dataclass
class FakeOrderResult:
    retcode: int = 10009
    order: int = 3001
    price: float = 1.08512
    volume: float = 0.01
    comment: str = "Done"


# ------------------------------------------------------------------ the gateway
class FakeMetaTrader5:
    """Configurable fake. Set the behavior attributes to simulate failures."""

    # --- constants, matching the official package's values
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    ORDER_TYPE_BUY_STOP = 4
    ORDER_TYPE_SELL_STOP = 5

    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_SLTP = 6
    TRADE_ACTION_REMOVE = 8

    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    SYMBOL_FILLING_FOK = 1
    SYMBOL_FILLING_IOC = 2
    SYMBOL_FILLING_RETURN = 4

    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_PLACED = 10008
    TRADE_RETCODE_REJECT = 10004
    TRADE_RETCODE_INVALID_FILL = 10030

    TIMEFRAME_M1 = 1
    TIMEFRAME_M5 = 5
    TIMEFRAME_M15 = 15
    TIMEFRAME_H1 = 16385
    TIMEFRAME_H4 = 16388
    TIMEFRAME_D1 = 16408

    def __init__(
        self,
        account: Optional[FakeAccount] = None,
        symbols: Optional[List[FakeSymbol]] = None,
        tick: Optional[FakeTick] = None,
        positions: Optional[List[FakePosition]] = None,
        orders: Optional[List[FakeOrder]] = None,
        initialize_ok: bool = True,
        login_ok: bool = True,
        terminal_connected: bool = True,
        order_retcode: int = TRADE_RETCODE_DONE,
        account_info_none: bool = False,
    ) -> None:
        self.account = account or FakeAccount()
        default_syms = [
            FakeSymbol(name="EURUSD"),
            FakeSymbol(
                name="BTCUSD",
                digits=2,
                point=0.01,
                trade_tick_size=0.01,
                trade_tick_value=0.01,
                trade_contract_size=1.0,
                volume_min=0.01,
                volume_max=1.0,
                volume_step=0.01,
                currency_base="USD",
                currency_profit="USD",
                currency_margin="USD",
                spread=2000,
            )
        ]
        self._symbols = symbols if symbols is not None else default_syms
        self.tick_value = tick or FakeTick()
        self.positions_value = positions if positions is not None else []
        self.orders_value = orders if orders is not None else []

        self.initialize_ok = initialize_ok
        self.login_ok = login_ok
        self.terminal_connected = terminal_connected
        self.order_retcode = order_retcode
        self.account_info_none = account_info_none

        self.calls: Dict[str, int] = {}
        self.login_calls: List[Dict[str, Any]] = []
        self.order_requests: List[Dict[str, Any]] = []
        self.initialize_kwargs: List[Dict[str, Any]] = []
        self.selected_symbols: List[str] = []
        self.shutdown_count = 0
        self._last_error: tuple = (0, "no error")
        self._tick_calls = 0
        self.order_ticket_seq = 3000

    # ------------------------------------------------------------- call log
    def _record(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    def call_count(self, name: str) -> int:
        return self.calls.get(name, 0)

    # ---------------------------------------------------------- MT5 surface
    def initialize(self, **kwargs: Any) -> bool:
        self._record("initialize")
        self.initialize_kwargs.append(dict(kwargs))
        if not self.initialize_ok:
            self._last_error = (-10003, "IPC initialize failed")
        return self.initialize_ok

    def login(self, login: int = 0, password: str = "", server: str = "") -> bool:
        self._record("login")
        self.login_calls.append({"login": login, "password": password, "server": server})
        if not self.login_ok:
            self._last_error = (-6, "authorization failed")
        return self.login_ok

    def shutdown(self) -> None:
        self._record("shutdown")
        self.shutdown_count += 1

    def last_error(self) -> tuple:
        return self._last_error

    def terminal_info(self) -> Optional[FakeTerminalInfo]:
        self._record("terminal_info")
        return FakeTerminalInfo(connected=self.terminal_connected)

    def account_info(self) -> Optional[FakeAccount]:
        self._record("account_info")
        if self.account_info_none:
            return None
        return self.account

    def symbols_get(self, group: Optional[str] = None) -> List[FakeSymbol]:
        self._record("symbols_get")
        return list(self._symbols)

    def symbol_info(self, name: str) -> Optional[FakeSymbol]:
        self._record("symbol_info")
        for sym in self._symbols:
            if sym.name == name:
                return sym
        return None

    def symbol_select(self, name: str, enable: bool = True) -> bool:
        self._record("symbol_select")
        self.selected_symbols.append(name)
        for sym in self._symbols:
            if sym.name == name:
                sym.visible = enable
                return True
        return False

    def symbol_info_tick(self, name: str) -> Optional[FakeTick]:
        self._record("symbol_info_tick")
        self._tick_calls += 1
        for sym in self._symbols:
            if sym.name == name:
                if "BTC" in name:
                    return FakeTick(bid=84120.0, ask=84145.0, last=84130.0)
                return self.tick_value
        return None

    def positions_get(self, symbol: Optional[str] = None, ticket: Optional[int] = None):
        self._record("positions_get")
        out = list(self.positions_value)
        if symbol is not None:
            out = [p for p in out if p.symbol == symbol]
        if ticket is not None:
            out = [p for p in out if p.ticket == ticket]
        return tuple(out)

    def orders_get(self, symbol: Optional[str] = None):
        self._record("orders_get")
        out = list(self.orders_value)
        if symbol is not None:
            out = [o for o in out if o.symbol == symbol]
        return tuple(out)

    def copy_rates_from_pos(self, symbol: str, timeframe: int, start_pos: int, count: int):
        self._record("copy_rates_from_pos")
        import numpy as np
        now = int(time.time())
        dtype = [
            ("time", "<i8"),
            ("open", "<f8"),
            ("high", "<f8"),
            ("low", "<f8"),
            ("close", "<f8"),
            ("tick_volume", "<u8"),
            ("spread", "<i4"),
            ("real_volume", "<u8"),
        ]
        rates = np.zeros(count, dtype=dtype)
        is_btc = "BTC" in symbol.upper()
        base = 84100.0 if is_btc else 1.08500
        step = 60 * 15
        for i in range(count):
            t = now - (count - i) * step
            if is_btc:
                c = base + (i * 20.0) + (35.0 if (i % 3 == 0) else -15.0)
                rates[i] = (t, c - 15.0, c + 40.0, c - 25.0, c, 100, 2000, 0)
            else:
                # Generate mild upward drift with pullbacks so indicators have enough movement
                c = base + (i * 0.00008) + (0.00015 if (i % 3 == 0) else -0.00005)
                rates[i] = (t, c - 0.0001, c + 0.00025, c - 0.00015, c, 100, 12, 0)
        return rates

    def order_send(self, request: Dict[str, Any]) -> FakeOrderResult:
        self._record("order_send")
        self.order_requests.append(dict(request))
        if self.order_retcode != self.TRADE_RETCODE_DONE:
            return FakeOrderResult(
                retcode=self.order_retcode,
                order=0,
                price=0.0,
                volume=0.0,
                comment="rejected by fake",
            )
        price = float(request.get("price", 1.08512))
        vol = float(request.get("volume", 0.01))

        # Handle position closure
        if "position" in request:
            ticket = int(request["position"])
            self.positions_value = [p for p in self.positions_value if getattr(p, "ticket", 0) != ticket]
            return FakeOrderResult(
                retcode=self.TRADE_RETCODE_DONE,
                order=ticket,
                price=price,
                volume=vol,
                comment="closed",
            )

        # Handle new position opening
        self.order_ticket_seq += 1
        ticket = self.order_ticket_seq
        pos_type = 0 if request.get("type", 0) == self.ORDER_TYPE_BUY else 1
        new_pos = FakePosition(
            ticket=ticket,
            symbol=str(request.get("symbol", "EURUSD")),
            type=pos_type,
            volume=vol,
            price_open=price,
            price_current=price,
            sl=float(request.get("sl", 0.0)),
            tp=float(request.get("tp", 0.0)),
            profit=0.0,
            time=int(time.time()),
        )
        self.positions_value.append(new_pos)
        return FakeOrderResult(
            retcode=self.order_retcode,
            order=ticket,
            price=price,
            volume=vol,
            comment="done",
        )


# ----------------------------------------------------------------- convenience
def make_fake(env_login: int = 12345678, **kwargs: Any) -> FakeMetaTrader5:
    """A fake whose account matches the given login."""
    account = kwargs.pop("account", None) or FakeAccount(login=env_login)
    return FakeMetaTrader5(account=account, **kwargs)


def fakes_env(login: str = "12345678", password: str = "trading-pw",
              server: str = "FundingPips-Trial", **extra: str) -> Dict[str, str]:
    """Build an env mapping for FundingPipsConfig."""
    env = {
        "FUNDINGPIPS_MT5_LOGIN": login,
        "FUNDINGPIPS_MT5_PASSWORD": password,
        "FUNDINGPIPS_MT5_SERVER": server,
    }
    env.update(extra)
    return env

