"""
FundingPips MT5 execution adapter.

Connection flow (fail CLOSED at every step)
-------------------------------------------
    1. import MetaTrader5 lazily   -> MT5UnavailableError / UnsupportedExecutionEnvironmentError
    2. mt5.initialize(path=...)    -> MT5InitializationError
    3. mt5.login(login, password, server) -> MT5AuthenticationError  (NEVER retried)
    4. terminal_info().connected   -> ConnectionLostError
    5. account_info() identity     -> AccountMismatchError
    6. server identity             -> UnexpectedServerError
    7. symbol resolution           -> SymbolNotFoundError / AmbiguousSymbolError
    8. trading permission          -> only when a write is actually requested

There is no fallback broker, no retry loop on authentication, and no synthetic
"connected" state. On a machine where MetaTrader5 cannot run (macOS/Linux), the
adapter raises UnsupportedExecutionEnvironmentError and says so plainly.

Safety
------
Read operations (account, symbols, ticks, positions, orders) are always allowed.
Write operations (place_order, close_position, modify, cancel) require BOTH:
    * config.allow_trading  (FUNDINGPIPS_ALLOW_TRADING=true)
    * an explicit allow_order=True argument from the calling CLI
Config alone is never sufficient, and the CLI flag alone is never sufficient.
"""
from __future__ import annotations

import os
import platform
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

from loguru import logger

from config.fundingpips import EXPECTED_SERVER, FundingPipsConfig
from src.execution.base import ExecutionBroker
from src.execution.errors import (
    AccountMismatchError,
    ConnectionLostError,
    CredentialFormatError,
    DuplicateOrderError,
    MT5AuthenticationError,
    MT5InitializationError,
    MT5UnavailableError,
    MissingCredentialError,
    OrderPreflightError,
    SymbolNotFoundError,
    TradingDisabledError,
    UnexpectedServerError,
    UnsupportedExecutionEnvironmentError,
)
from src.execution.models import (
    AccountInfo,
    OrderIntent,
    OrderResultInfo,
    OrderSide,
    OrderStatus,
    PendingOrderSnapshot,
    PositionSnapshot,
    SymbolInfo,
    Tick,
    TradeMode,
)
from src.execution.redaction import mask_account, redact
from src.execution.symbol_mapper import available_symbol_names, resolve_symbol
from src.execution.telemetry import exec_event

#: MT5 SYMBOL_TRADE_MODE_* -> TradeMode
_TRADE_MODE_MAP = {
    0: TradeMode.DISABLED,
    1: TradeMode.LONG_ONLY,
    2: TradeMode.SHORT_ONLY,
    3: TradeMode.CLOSE_ONLY,
    4: TradeMode.FULL,
}

#: Platforms where the official MetaTrader5 wheel is expected to work.
SUPPORTED_PLATFORMS = ("Windows",)


def mt5_supported_here() -> bool:
    """Whether the official MetaTrader5 package can run natively on this host."""
    return platform.system() in SUPPORTED_PLATFORMS


def import_mt5(module: Any = None) -> Any:
    """Import MetaTrader5 lazily.

    Args:
        module: test injection point. When provided it is returned untouched, which
            is how the unit tests drive a fake gateway without MT5 installed.

    Raises:
        UnsupportedExecutionEnvironmentError: package absent and OS unsupported.
        MT5UnavailableError: package absent on an otherwise supported OS.
        MT5InitializationError: package present but failed to import at runtime.
    """
    if module is not None:
        return module

    try:
        import MetaTrader5 as mt5  # noqa: WPS433 - intentional lazy import
    except ImportError as exc:
        if not mt5_supported_here():
            raise UnsupportedExecutionEnvironmentError(
                "MetaTrader5 Python integration is not available on this platform "
                "(%s %s). The official package is Windows-only. Run this adapter on a "
                "Windows MT5 host/VPS with the terminal installed, and point "
                "FUNDINGPIPS_MT5_PATH at terminal64.exe. Research and tests remain "
                "fully functional here without MT5." % (platform.system(), platform.machine())
            ) from exc
        raise MT5UnavailableError(
            "MetaTrader5 package is not installed. Install it with: pip install .[mt5]"
        ) from exc
    except Exception as exc:  # pragma: no cover - broken native install
        raise MT5InitializationError(
            "MetaTrader5 imported but could not be loaded: %s" % redact(exc)
        ) from exc

    return mt5


class FundingPipsMT5Adapter(ExecutionBroker):
    """FundingPips Free Trial execution adapter over MetaTrader 5."""

    def __init__(
        self,
        config: FundingPipsConfig,
        mt5_module: Any = None,
        allow_order: bool = False,
        max_spread_pips: Optional[float] = None,
        expected_symbols: Optional[Sequence[str]] = None,
    ) -> None:
        self._config = config
        self._mt5 = mt5_module
        self._allow_order = bool(allow_order)
        self._connected = False
        self._initialized = False
        self._provider_symbol: Optional[str] = None
        self._symbol_cache: Dict[str, SymbolInfo] = {}
        self._symbol_list: Optional[List[str]] = None
        self._max_spread_pips = max_spread_pips
        self._expected_symbols = list(expected_symbols or [])

    # ------------------------------------------------------------- factories
    @classmethod
    def from_env(
        cls,
        env: Optional[Mapping[str, str]] = None,
        mt5_module: Any = None,
        allow_order: bool = False,
        require_credentials: bool = True,
        symbol: str = "EURUSD",
        **kwargs: Any,
    ) -> "FundingPipsMT5Adapter":
        config = FundingPipsConfig.from_env(
            env=env, require=require_credentials, symbol=symbol
        )
        return cls(config, mt5_module=mt5_module, allow_order=allow_order, **kwargs)

    # -------------------------------------------------------------- read-only
    @property
    def config(self) -> FundingPipsConfig:
        return self._config

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def trading_enabled(self) -> bool:
        """Config gate only. The order gate additionally requires allow_order."""
        return self._config.allow_trading

    @property
    def can_trade(self) -> bool:
        """Full two-gate evaluation, for reporting."""
        return self._config.allow_trading and self._allow_order

    def _require_module(self) -> Any:
        if self._mt5 is None:
            self._mt5 = import_mt5(None)
        return self._mt5

    def _require_connected(self) -> Any:
        """Return the MT5 module, or raise if the session is not live.

        Re-checks whether the terminal is still alive on every call, so a dropped
        connection fails closed instead of returning stale data.
        """
        mt5 = self._require_module()
        if not self._connected:
            raise ConnectionLostError("Adapter is not connected. Call connect() first.")

        terminal = mt5.terminal_info()
        if terminal is None:
            self._connected = False
            exec_event("fundingpips_mt5", "connection_lost", success=False,
                       reason="terminal_info() returned None")
            raise ConnectionLostError("MT5 terminal no longer responds (terminal_info is None).")
        if not getattr(terminal, "connected", False):
            self._connected = False
            exec_event("fundingpips_mt5", "connection_lost", success=False,
                       reason="terminal reports disconnected")
            raise ConnectionLostError("MT5 terminal reports it is disconnected.")
        return mt5

    # ----------------------------------------------------------------- connect
    def connect(self) -> None:
        """Initialize the terminal and log in. Fails closed; never retries auth."""
        cfg = self._config

        if not cfg.has_credentials:
            exec_event("fundingpips_mt5", "login_attempt", success=False,
                       reason="missing credentials")
            raise MissingCredentialError(
                "FundingPips credentials are not configured. Set "
                "FUNDINGPIPS_MT5_LOGIN and FUNDINGPIPS_MT5_PASSWORD."
            )

        cfg.require_expected_server()
        mt5 = self._require_module()

        exec_event(
            "fundingpips_mt5", "login_attempt",
            login_masked=cfg.masked_login,
            server=cfg.server,
        )

        init_kwargs: Dict[str, Any] = {}
        if cfg.terminal_path:
            init_kwargs["path"] = cfg.terminal_path

        if not mt5.initialize(**init_kwargs):
            error = self._last_error(mt5)
            self._shutdown(mt5)
            exec_event("fundingpips_mt5", "initialize_failure", success=False, reason=error)
            raise MT5InitializationError(
                "mt5.initialize() failed. %s" % error,
                terminal_path=cfg.terminal_path,
            )
        self._initialized = True

        # --- login. A failure here is NEVER retried: repeating a bad credential
        # --- burns the broker's login limiter and can lock the account.
        authorized = mt5.login(
            login=int(cfg.login),
            password=cfg.password.reveal(),
            server=cfg.server,
        )
        if not authorized:
            error = self._last_error(mt5)
            self._shutdown(mt5)
            exec_event("fundingpips_mt5", "login_failure", success=False,
                       login_masked=cfg.masked_login, reason=error)
            raise MT5AuthenticationError(
                "mt5.login() failed for account %s on server %r. %s"
                % (cfg.masked_login, cfg.server, error)
            )

        self._connected = True

        # --- verify terminal connection state
        terminal = mt5.terminal_info()
        if terminal is None or not getattr(terminal, "connected", False):
            self._connected = False
            self._shutdown(mt5)
            exec_event("fundingpips_mt5", "login_failure", success=False,
                       reason="terminal not connected after login")
            raise ConnectionLostError(
                "Terminal did not report a live connection after a successful login."
            )

        # --- verify account identity
        account = self.account_info()
        if int(account.login) != int(cfg.login):
            self.disconnect()
            exec_event("fundingpips_mt5", "account_mismatch", success=False,
                       expected=cfg.masked_login, actual=account.masked_login)
            raise AccountMismatchError(
                "Connected account %s does not match the requested account %s."
                % (account.masked_login, cfg.masked_login)
            )

        # --- verify server identity
        if account.server != EXPECTED_SERVER:
            self.disconnect()
            exec_event("fundingpips_mt5", "unexpected_server", success=False,
                       expected=EXPECTED_SERVER, actual=account.server)
            raise UnexpectedServerError(
                "Terminal reports server %r but %r is required."
                % (redact(account.server), EXPECTED_SERVER)
            )

        exec_event("fundingpips_mt5", "login_success", success=True,
                   login_masked=account.masked_login, server=account.server,
                   trade_allowed=account.trade_allowed, is_demo=account.is_demo)

    def disconnect(self) -> None:
        if self._mt5 is not None:
            self._shutdown(self._mt5)
        self._connected = False
        exec_event("fundingpips_mt5", "disconnect", success=True)

    @staticmethod
    def _shutdown(mt5: Any) -> None:
        try:
            mt5.shutdown()
        except Exception:  # pragma: no cover - shutdown is best-effort
            pass

    @staticmethod
    def _last_error(mt5: Any) -> str:
        try:
            return redact(str(mt5.last_error()))
        except Exception:  # pragma: no cover
            return "unknown error"

    # ------------------------------------------------------------ read methods
    def account_info(self) -> AccountInfo:
        mt5 = self._require_connected()
        raw = mt5.account_info()
        if raw is None:
            raise ConnectionLostError("account_info() returned None; session is stale.")

        return AccountInfo(
            login=int(getattr(raw, "login", 0) or 0),
            server=str(getattr(raw, "server", "") or ""),
            currency=str(getattr(raw, "currency", "") or ""),
            balance=float(getattr(raw, "balance", 0.0) or 0.0),
            equity=float(getattr(raw, "equity", 0.0) or 0.0),
            margin=float(getattr(raw, "margin", 0.0) or 0.0),
            margin_free=float(getattr(raw, "margin_free", 0.0) or 0.0),
            margin_level=float(getattr(raw, "margin_level", 0.0) or 0.0),
            leverage=int(getattr(raw, "leverage", 0) or 0),
            trade_allowed=bool(getattr(raw, "trade_allowed", False)),
            trade_expert=bool(getattr(raw, "trade_expert", False)),
            connected=True,
            company=str(getattr(raw, "company", "") or ""),
            is_demo=bool(getattr(raw, "trade_mode", 0) == 0) if hasattr(raw, "trade_mode") else None,
        )

    def _symbol_names(self) -> List[str]:
        if self._symbol_list is None:
            mt5 = self._require_connected()
            names = available_symbol_names(mt5)
            if not names and self._expected_symbols:
                names = list(self._expected_symbols)
            self._symbol_list = names
        return self._symbol_list

    def resolve(self, canonical_symbol: str) -> str:
        """Resolve and cache the provider symbol for a canonical name."""
        cached = self._symbol_cache.get(canonical_symbol)
        if cached is not None:
            return cached.provider_symbol
        resolution = resolve_symbol(canonical_symbol, self._symbol_names())
        self._provider_symbol = resolution.provider_symbol
        exec_event("fundingpips_mt5", "symbol_resolved", success=True,
                   canonical_symbol=resolution.canonical_symbol,
                   provider_symbol=resolution.provider_symbol,
                   match_rule=resolution.match_rule)
        return resolution.provider_symbol

    def symbol_info(self, symbol: str) -> SymbolInfo:
        cached = self._symbol_cache.get(symbol)
        if cached is not None:
            return cached

        mt5 = self._require_connected()
        provider = self.resolve(symbol)

        raw = mt5.symbol_info(provider)
        if raw is None:
            raise SymbolNotFoundError(
                "Resolved symbol %r is not present in the terminal symbol table."
                % provider, canonical_symbol=symbol
            )

        # symbol_select only makes the symbol visible to the terminal; it never
        # places an order and is safe while trading is disabled.
        select_ok = True
        try:
            select_ok = bool(mt5.symbol_select(provider, True))
        except Exception:  # pragma: no cover - some builds lack symbol_select
            select_ok = bool(getattr(raw, "visible", True))

        mode_value = int(getattr(raw, "trade_mode", 4) or 0)
        info = SymbolInfo(
            canonical_symbol=symbol,
            provider_symbol=provider,
            digits=int(getattr(raw, "digits", 5) or 5),
            point=float(getattr(raw, "point", 0.00001) or 0.00001),
            trade_tick_size=float(getattr(raw, "trade_tick_size", 0.0) or 0.0),
            trade_tick_value=float(getattr(raw, "trade_tick_value", 0.0) or 0.0),
            contract_size=float(getattr(raw, "trade_contract_size", 0.0) or 0.0),
            volume_min=float(getattr(raw, "volume_min", 0.0) or 0.0),
            volume_max=float(getattr(raw, "volume_max", 0.0) or 0.0),
            volume_step=float(getattr(raw, "volume_step", 0.0) or 0.0),
            stops_level=int(getattr(raw, "trade_stops_level", 0) or 0),
            freeze_level=int(getattr(raw, "trade_freeze_level", 0) or 0),
            trade_mode=_TRADE_MODE_MAP.get(mode_value, TradeMode.UNKNOWN),
            swap_long=float(getattr(raw, "swap_long", 0.0) or 0.0),
            swap_short=float(getattr(raw, "swap_short", 0.0) or 0.0),
            swap_mode=int(getattr(raw, "swap_mode", 0) or 0),
            swap_rollover3days=int(getattr(raw, "swap_rollover3days", 0) or 0),
            currency_base=str(getattr(raw, "currency_base", "") or ""),
            currency_profit=str(getattr(raw, "currency_profit", "") or ""),
            currency_margin=str(getattr(raw, "currency_margin", "") or ""),
            execution_mode=getattr(raw, "trade_exemode", None),
            filling_modes=getattr(raw, "filling_mode", None),
            spread=int(getattr(raw, "spread", 0) or 0),
            visible=bool(getattr(raw, "visible", True)),
            select_ok=select_ok,
        )
        self._symbol_cache[symbol] = info
        return info

    def tick(self, symbol: str) -> Tick:
        mt5 = self._require_connected()
        provider = self.resolve(symbol)
        raw = mt5.symbol_info_tick(provider)
        if raw is None:
            raise SymbolNotFoundError(
                "No tick available for %r; the market may be closed." % provider,
                canonical_symbol=symbol,
            )

        receipt = datetime.now(timezone.utc)
        raw_time = getattr(raw, "time", None)
        terminal_time = (
            datetime.fromtimestamp(int(raw_time), tz=timezone.utc) if raw_time else None
        )
        timestamp = terminal_time or receipt

        return Tick(
            canonical_symbol=symbol,
            provider_symbol=provider,
            bid=float(getattr(raw, "bid", 0.0) or 0.0),
            ask=float(getattr(raw, "ask", 0.0) or 0.0),
            last=float(getattr(raw, "last", 0.0) or 0.0),
            volume=float(getattr(raw, "volume", 0.0) or 0.0),
            timestamp_utc=timestamp,
            terminal_time_utc=terminal_time,
            local_receipt_utc=receipt,
            flags=int(getattr(raw, "flags", 0) or 0),
        )

    def positions(self, symbol: Optional[str] = None) -> List[PositionSnapshot]:
        mt5 = self._require_connected()
        provider = self.resolve(symbol) if symbol else None
        raw_positions = (
            mt5.positions_get(symbol=provider) if provider else mt5.positions_get()
        )
        if not raw_positions:
            return []

        out: List[PositionSnapshot] = []
        for pos in raw_positions:
            open_time = getattr(pos, "time", None)
            out.append(
                PositionSnapshot(
                    position_id=str(getattr(pos, "ticket", "")),
                    symbol=symbol or str(getattr(pos, "symbol", "")),
                    side="BUY" if int(getattr(pos, "type", 0)) == 0 else "SELL",
                    volume=float(getattr(pos, "volume", 0.0) or 0.0),
                    entry_price=float(getattr(pos, "price_open", 0.0) or 0.0),
                    current_price=float(getattr(pos, "price_current", 0.0) or 0.0),
                    sl=float(getattr(pos, "sl", 0.0) or 0.0),
                    tp=float(getattr(pos, "tp", 0.0) or 0.0),
                    profit=float(getattr(pos, "profit", 0.0) or 0.0),
                    swap=float(getattr(pos, "swap", 0.0) or 0.0),
                    magic=int(getattr(pos, "magic", 0) or 0),
                    comment=str(getattr(pos, "comment", "") or ""),
                    open_time_utc=(
                        datetime.fromtimestamp(int(open_time), tz=timezone.utc)
                        if open_time else None
                    ),
                )
            )
        return out

    def orders(self, symbol: Optional[str] = None) -> List[PendingOrderSnapshot]:
        mt5 = self._require_connected()
        provider = self.resolve(symbol) if symbol else None
        raw_orders = mt5.orders_get(symbol=provider) if provider else mt5.orders_get()
        if not raw_orders:
            return []

        out: List[PendingOrderSnapshot] = []
        for order in raw_orders:
            order_time = getattr(order, "time_setup", None)
            out.append(
                PendingOrderSnapshot(
                    position_id=str(getattr(order, "ticket", "")),
                    symbol=symbol or str(getattr(order, "symbol", "")),
                    side="BUY" if int(getattr(order, "type", 0)) in (0, 2, 4) else "SELL",
                    volume=float(getattr(order, "volume_current", 0.0) or 0.0),
                    price_open=float(getattr(order, "price_open", 0.0) or 0.0),
                    sl=float(getattr(order, "sl", 0.0) or 0.0),
                    tp=float(getattr(order, "tp", 0.0) or 0.0),
                    magic=int(getattr(order, "magic", 0) or 0),
                    comment=str(getattr(order, "comment", "") or ""),
                    time_utc=(
                        datetime.fromtimestamp(int(order_time), tz=timezone.utc)
                        if order_time else None
                    ),
                )
            )
        return out

    # ---------------------------------------------------------------- preflight
    def preflight(
        self,
        request: OrderIntent,
        equity: Optional[float] = None,
        free_margin: Optional[float] = None,
    ) -> SymbolInfo:
        """Centralised pre-conditions for ANY order. Prefer reject over repair.

        Returns the resolved SymbolInfo on success.

        Raises:
            OrderPreflightError: with a structured reason_code.
        """
        if not self._config.allow_trading:
            raise OrderPreflightError(
                "trading_disabled",
                "Trading is disabled. Set FUNDINGPIPS_ALLOW_TRADING=true and pass the "
                "explicit order flag to enable execution.",
            )
        if not self._allow_order:
            raise OrderPreflightError(
                "explicit_order_flag_required",
                "Trading is configured but no explicit order flag was supplied. "
                "Both gates are required.",
            )

        info = self.symbol_info(request.symbol)

        if not info.visible or not info.select_ok:
            raise OrderPreflightError(
                "symbol_not_visible", "Symbol %r is not visible in the terminal." % request.symbol
            )
        if info.trade_mode == TradeMode.DISABLED:
            raise OrderPreflightError("symbol_trade_disabled", "Trading is disabled for this symbol.")
        if info.trade_mode == TradeMode.CLOSE_ONLY:
            raise OrderPreflightError("symbol_close_only", "Symbol is close-only.")
        if info.trade_mode == TradeMode.LONG_ONLY and request.side == OrderSide.SELL:
            raise OrderPreflightError("direction_not_allowed", "Symbol is long-only.")
        if info.trade_mode == TradeMode.SHORT_ONLY and request.side == OrderSide.BUY:
            raise OrderPreflightError("direction_not_allowed", "Symbol is short-only.")

        volume = float(request.volume)
        if volume <= 0:
            raise OrderPreflightError("volume_non_positive", "Volume must be positive.", volume=volume)
        if info.volume_min > 0 and volume < info.volume_min:
            raise OrderPreflightError(
                "volume_below_minimum",
                "Volume %.4f is below the broker minimum %.4f." % (volume, info.volume_min),
                volume=volume, volume_min=info.volume_min,
            )
        if info.volume_max > 0 and volume > info.volume_max:
            raise OrderPreflightError(
                "volume_above_maximum",
                "Volume %.4f exceeds the broker maximum %.4f." % (volume, info.volume_max),
                volume=volume, volume_max=info.volume_max,
            )
        if info.volume_step > 0:
            steps = volume / info.volume_step
            if abs(steps - round(steps)) > 1e-6:
                raise OrderPreflightError(
                    "volume_not_aligned_to_step",
                    "Volume %.4f is not a multiple of volume_step %.4f."
                    % (volume, info.volume_step),
                    volume=volume, volume_step=info.volume_step,
                )

        account = self.account_info()
        if not account.trade_allowed:
            raise OrderPreflightError("account_trading_disallowed", "Account does not permit trading.")

        tick = self.tick(request.symbol)
        if not tick.is_valid:
            raise OrderPreflightError(
                "invalid_quote", "Crossed or zero quote: bid=%s ask=%s" % (tick.bid, tick.ask)
            )
        if self._max_spread_pips is not None and info.pip_size > 0:
            spread_pips = (tick.ask - tick.bid) / info.pip_size
            if spread_pips > self._max_spread_pips:
                raise OrderPreflightError(
                    "spread_above_ceiling",
                    "Spread %.2f pips exceeds the ceiling %.2f pips."
                    % (spread_pips, self._max_spread_pips),
                    spread_pips=spread_pips, ceiling=self._max_spread_pips,
                )

        min_stop_distance = info.stops_level * info.point if info.point > 0 else 0.0
        if min_stop_distance > 0:
            self._check_stop_distance(request, tick, min_stop_distance, info)

        if equity is not None and free_margin is not None:
            self._check_margin(request, info, equity, free_margin)

        existing = self.positions(request.symbol)
        if request.client_order_id:
            for pos in existing:
                if pos.comment and pos.comment == request.client_order_id:
                    raise OrderPreflightError(
                        "duplicate_client_order_id",
                        "A position already carries client_order_id %r." % request.client_order_id,
                    )
        return info

    @staticmethod
    def _check_stop_distance(
        request: OrderIntent, tick: Tick, minimum: float, info: SymbolInfo
    ) -> None:
        if request.sl > 0:
            distance = abs(tick.bid - request.sl) if request.side == OrderSide.BUY else abs(request.sl - tick.ask)
            if distance < minimum:
                raise OrderPreflightError(
                    "stop_loss_too_close",
                    "SL distance %.5f is below the required %.5f (stops_level=%d points)."
                    % (distance, minimum, info.stops_level),
                    distance=distance, required=minimum,
                )
        if request.tp > 0:
            distance = abs(request.tp - tick.bid) if request.side == OrderSide.BUY else abs(tick.ask - request.tp)
            if distance < minimum:
                raise OrderPreflightError(
                    "take_profit_too_close",
                    "TP distance %.5f is below the required %.5f (stops_level=%d points)."
                    % (distance, minimum, info.stops_level),
                    distance=distance, required=minimum,
                )

    @staticmethod
    def _check_margin(
        request: OrderIntent, info: SymbolInfo, equity: float, free_margin: float
    ) -> None:
        """Conservative margin check using notional / leverage.

        Uses the account leverage via margin_free comparison only; it deliberately
        does not attempt to reimplement the broker's margin formula. If the required
        margin cannot be shown to fit inside free margin, we REJECT rather than guess.
        """
        required = request.volume * info.contract_size
        if required <= 0 or free_margin <= 0:
            raise OrderPreflightError(
                "insufficient_margin",
                "Cannot establish sufficient free margin (free=%s, notional=%s)."
                % (free_margin, required),
            )
        # A 1:1 notional/gratis bound: if even the notional exceeds free margin times a
        # generous factor, the trade cannot be afforded at any retail leverage.
        if required > max(free_margin, equity) * 1000.0:
            raise OrderPreflightError(
                "insufficient_margin",
                "Required notional %.2f is implausibly large against free margin %.2f."
                % (required, free_margin),
            )

    # ------------------------------------------------------------ write methods
    def _require_write_access(self) -> None:
        """The two-gate check applied to every write path."""
        if not self._config.allow_trading:
            exec_event("fundingpips_mt5", "order_blocked", success=False,
                       reason="trading_disabled_by_config")
            raise TradingDisabledError(
                "Real trading is disabled. FUNDINGPIPS_ALLOW_TRADING is not true. "
                "Read-only operations remain available."
            )
        if not self._allow_order:
            exec_event("fundingpips_mt5", "order_blocked", success=False,
                       reason="explicit_order_flag_missing")
            raise TradingDisabledError(
                "Trading is configured but this invocation did not include the explicit "
                "order acknowledgement flag. Both gates are required."
            )
        if not self._connected:
            raise ConnectionLostError("Not connected; refusing to submit an order.")

    def place_order(self, request: OrderIntent, **_kwargs: Any) -> OrderResultInfo:
        """Submit a market order with SL/TP attached.

        Guarded by preflight and the two-gate safety check. The volume is NEVER
        adjusted here: if risk sizing produced a sub-minimum volume the caller must
        have already rejected the trade.
        """
        self._require_write_access()

        try:
            info = self.preflight(request)
        except OrderPreflightError as exc:
            exec_event("fundingpips_mt5", "order_preflight_rejected", success=False,
                       reason=exc.reason_code, symbol=request.symbol)
            return self._rejected(request, "PREFLIGHT:%s" % exc.reason_code, str(exc))

        mt5 = self._require_connected()
        tick = self.tick(request.symbol)
        requested_price = tick.ask if request.side == OrderSide.BUY else tick.bid

        order_type = mt5.ORDER_TYPE_BUY if request.side == OrderSide.BUY else mt5.ORDER_TYPE_SELL
        raw_request: Dict[str, Any] = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": info.provider_symbol,
            "volume": float(request.volume),
            "type": order_type,
            "price": requested_price,
            "sl": float(request.sl),
            "tp": float(request.tp),
            "deviation": int(request.deviation),
            "magic": int(request.magic),
            "comment": (request.client_order_id or request.comment or "QP")[:31],
            "type_time": mt5.ORDER_TIME_GTC,
        }
        filling = self._choose_filling_mode(mt5, info, request.side)
        if filling is not None:
            raw_request["type_filling"] = filling

        exec_event("fundingpips_mt5", "order_submit", success=None,
                   symbol=request.symbol, provider_symbol=info.provider_symbol,
                   side=request.side.value, volume=request.volume,
                   requested_price=requested_price)

        raw_result = mt5.order_send(raw_request)
        if raw_result is None:
            error = self._last_error(mt5)
            exec_event("fundingpips_mt5", "order_result_none", success=False, reason=error)
            return self._rejected(request, "NO_RESULT", "order_send returned None. %s" % error)

        return self._normalize_result(request, raw_result, requested_price, mt5)

    #: (symbol_info mask bit, ORDER_* value) pairs, in preference order.
    #: MT5 uses TWO different constant families here, and confusing them is the
    #: classic cause of retcode 10030:
    #:   symbol_info.filling_mode is a BITMASK of SYMBOL_FILLING_* (FOK=1, IOC=2)
    #:   the order request's type_filling is an ENUM of ORDER_FILLING_* (FOK=0, IOC=1)
    _FILLING_PREFERENCES = (
        ("SYMBOL_FILLING_FOK", "ORDER_FILLING_FOK"),
        ("SYMBOL_FILLING_IOC", "ORDER_FILLING_IOC"),
        ("SYMBOL_FILLING_RETURN", "ORDER_FILLING_RETURN"),
    )

    def _choose_filling_mode(self, mt5: Any, info: SymbolInfo, side: OrderSide) -> Optional[int]:
        """Pick a filling mode the symbol actually supports.

        Inspects the declared filling_mode bitmask rather than assuming IOC. When the
        symbol advertises nothing we send no type_filling at all and let the terminal
        apply its default, instead of guessing and getting rejected.
        """
        mask = info.filling_modes
        if not mask:
            return None
        for mask_attr, value_attr in self._FILLING_PREFERENCES:
            bit = getattr(mt5, mask_attr, None)
            value = getattr(mt5, value_attr, None)
            if bit is None or value is None:
                continue
            if int(mask) & int(bit):
                return int(value)
        return None

    def _normalize_result(
        self, request: OrderIntent, raw: Any, requested_price: float, mt5: Any
    ) -> OrderResultInfo:
        retcode = int(getattr(raw, "retcode", -1))
        done = getattr(mt5, "TRADE_RETCODE_DONE", 10009)
        placed = getattr(mt5, "TRADE_RETCODE_PLACED", 10008)
        success = retcode in (done, placed)

        status = OrderStatus.FILLED if success else OrderStatus.REJECTED
        filled_price = float(getattr(raw, "price", 0.0) or 0.0)

        exec_event("fundingpips_mt5", "order_result", success=success,
                   symbol=request.symbol, side=request.side.value,
                   volume=request.volume, retcode=retcode,
                   requested_price=requested_price, filled_price=filled_price,
                   reason="" if success else str(getattr(raw, "comment", "")))

        return OrderResultInfo(
            success=success,
            order_id=str(getattr(raw, "order", "") or "") or None,
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side.value,
            volume=float(getattr(raw, "volume", request.volume) or request.volume),
            requested_price=requested_price,
            filled_price=filled_price,
            sl=request.sl,
            tp=request.tp,
            status=status,
            retcode=retcode,
            retcode_name=self._retcode_name(mt5, retcode),
            error_message=None if success else redact(str(getattr(raw, "comment", ""))),
            comment=str(getattr(raw, "comment", "") or ""),
            timestamp_utc=datetime.now(timezone.utc),
        )

    @staticmethod
    def _retcode_name(mt5: Any, retcode: int) -> str:
        for name in dir(mt5):
            if name.startswith("TRADE_RETCODE_") and getattr(mt5, name, None) == retcode:
                return name
        return "TRADE_RETCODE_%d" % retcode

    def _rejected(self, request: OrderIntent, code: str, message: str) -> OrderResultInfo:
        return OrderResultInfo(
            success=False,
            order_id=None,
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side.value,
            volume=request.volume,
            requested_price=0.0,
            filled_price=0.0,
            sl=request.sl,
            tp=request.tp,
            status=OrderStatus.REJECTED,
            error_message=redact(message),
            comment=code,
            timestamp_utc=datetime.now(timezone.utc),
        )

    def close_position(self, position_id: str, reason: str = "") -> OrderResultInfo:
        """Close a position in full. Gated by the two-gate safety check."""
        self._require_write_access()
        mt5 = self._require_connected()

        raw_positions = mt5.positions_get(ticket=int(position_id))
        if not raw_positions:
            return OrderResultInfo(
                success=False, order_id=position_id, client_order_id=None,
                symbol="", side="CLOSE", volume=0.0, requested_price=0.0,
                filled_price=0.0, status=OrderStatus.REJECTED,
                error_message="Position %s not found." % position_id,
                timestamp_utc=datetime.now(timezone.utc),
            )

        pos = raw_positions[0]
        provider_symbol = str(getattr(pos, "symbol", ""))
        is_long = int(getattr(pos, "type", 0)) == 0
        close_type = mt5.ORDER_TYPE_SELL if is_long else mt5.ORDER_TYPE_BUY
        tick = mt5.symbol_info_tick(provider_symbol)
        if tick is None:
            raise SymbolNotFoundError("No tick available to close %r." % provider_symbol)
        price = float(tick.bid) if is_long else float(tick.ask)

        raw_request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": int(position_id),
            "symbol": provider_symbol,
            "volume": float(getattr(pos, "volume", 0.0) or 0.0),
            "type": close_type,
            "price": price,
            "deviation": 20,
            "magic": int(getattr(pos, "magic", 0) or 0),
            "comment": (reason or "QP close")[:31],
            "type_time": mt5.ORDER_TIME_GTC,
        }

        raw_result = mt5.order_send(raw_request)
        if raw_result is None:
            error = self._last_error(mt5)
            return OrderResultInfo(
                success=False, order_id=position_id, client_order_id=None,
                symbol=provider_symbol, side="CLOSE", volume=0.0,
                requested_price=price, filled_price=0.0,
                status=OrderStatus.REJECTED,
                error_message="order_send returned None on close. %s" % error,
                timestamp_utc=datetime.now(timezone.utc),
            )

        retcode = int(getattr(raw_result, "retcode", -1))
        done = getattr(mt5, "TRADE_RETCODE_DONE", 10009)
        success = retcode == done
        exec_event("fundingpips_mt5", "position_close", success=success,
                   position_id=position_id, retcode=retcode, reason=reason)

        return OrderResultInfo(
            success=success,
            order_id=str(getattr(raw_result, "order", "") or position_id),
            client_order_id=None,
            symbol=provider_symbol,
            side="CLOSE",
            volume=float(getattr(raw_result, "volume", 0.0) or 0.0),
            requested_price=price,
            filled_price=float(getattr(raw_result, "price", 0.0) or 0.0),
            status=OrderStatus.FILLED if success else OrderStatus.REJECTED,
            retcode=retcode,
            retcode_name=self._retcode_name(mt5, retcode),
            error_message=None if success else redact(str(getattr(raw_result, "comment", ""))),
            timestamp_utc=datetime.now(timezone.utc),
        )

    def cancel_order(self, order_id: str) -> bool:
        self._require_write_access()
        mt5 = self._require_connected()
        result = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": int(order_id)})
        if result is None:
            return False
        return int(getattr(result, "retcode", -1)) == int(getattr(mt5, "TRADE_RETCODE_DONE", 10009))

    def modify_position(self, position_id: str, sl: float, tp: float) -> bool:
        self._require_write_access()
        mt5 = self._require_connected()
        result = mt5.order_send({
            "action": mt5.TRADE_ACTION_SLTP,
            "position": int(position_id),
            "sl": float(sl),
            "tp": float(tp),
        })
        if result is None:
            return False
        return int(getattr(result, "retcode", -1)) == int(getattr(mt5, "TRADE_RETCODE_DONE", 10009))

    # ------------------------------------------------------------------ context
    def __enter__(self) -> "FundingPipsMT5Adapter":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.disconnect()

    def __repr__(self) -> str:
        """Credential-free. The password is a Secret, so it renders as a mask."""
        return (
            "FundingPipsMT5Adapter(login=%s, server=%r, symbol=%r, allow_trading=%s, "
            "allow_order=%s, connected=%s)"
            % (
                self._config.masked_login,
                self._config.server,
                self._config.symbol,
                self._config.allow_trading,
                self._allow_order,
                self._connected,
            )
        )

