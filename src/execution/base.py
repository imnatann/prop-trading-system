"""
Execution interface (protocol) + legacy compatibility bridge.

Architectural rule enforced here
--------------------------------
Strategy, risk, and alpha modules depend on the ExecutionBroker PROTOCOL below.
They never import MetaTrader5 and never reference FundingPipsMT5Adapter by name.
The concrete adapter is chosen once, by configuration, in get_execution_broker().

The legacy src.broker.base.BaseBrokerAdapter ABC already existed and is still used
by the OMS dispatcher, the safety stack, and their tests. Rather than fork that
hierarchy (which would duplicate ~14 methods and risk breaking passing tests), the
legacy ABC is kept intact and a thin BRIDGE makes any ExecutionBroker usable
anywhere a BaseBrokerAdapter is expected. No existing behaviour changes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from src.execution.models import (
    AccountInfo,
    OrderIntent,
    OrderResultInfo,
    OrderSide,
    PendingOrderSnapshot,
    PositionSnapshot,
    SymbolInfo,
    Tick,
)
from src.broker.base import (
    BaseBrokerAdapter,
    OrderResult as LegacyOrderResult,
    PositionInfo as LegacyPositionInfo,
)
from src.broker.models import SymbolSpec
from src.execution.redaction import redact
from src.risk.drawdown_monitor import AccountSnapshot
from src.strategy.base import SignalAction


@runtime_checkable
class ExecutionBroker(Protocol):
    """The execution contract every broker adapter implements.

    Read-only capability is always available. Write capability is gated by the
    adapter's own safety policy (see FundingPipsMT5Adapter), NOT by this protocol.
    """

    def connect(self) -> None:
        """Establish the session. MUST fail closed, never fall back."""
        ...

    def disconnect(self) -> None:
        """Release the session. Safe to call when already disconnected."""
        ...

    def account_info(self) -> AccountInfo:
        """Current account snapshot, including identity used for verification."""
        ...

    def symbol_info(self, symbol: str) -> SymbolInfo:
        """Broker-native specification for a canonical symbol."""
        ...

    def tick(self, symbol: str) -> Tick:
        """Latest normalized top-of-book quote, UTC stamped."""
        ...

    def positions(self, symbol: Optional[str] = None) -> List[PositionSnapshot]:
        """Open positions, optionally filtered by canonical symbol."""
        ...

    def orders(self, symbol: Optional[str] = None) -> List[PendingOrderSnapshot]:
        """Working (pending) orders, optionally filtered by canonical symbol."""
        ...

    def place_order(self, request: OrderIntent) -> OrderResultInfo:
        """Submit an order. MUST raise TradingDisabledError when gated off."""
        ...

    def close_position(self, position_id: str, reason: str = "") -> OrderResultInfo:
        """Close one position by broker position id."""
        ...


class BrokerExecutionAdapter(BaseBrokerAdapter):
    """Legacy-ABC bridge over any :class:ExecutionBroker.

    Lets a modern adapter be driven by the existing OrderDispatcher, RiskGatekeeper,
    reconciliation, and kill-switch stack without those modules learning about MT5.
    """

    def __init__(self, broker: ExecutionBroker, magic: int = 888999) -> None:
        self._broker = broker
        self._magic = magic
        self._fill_cache: Dict[str, OrderResultInfo] = {}

    @property
    def wrapped(self) -> ExecutionBroker:
        return self._broker

    # ------------------------------------------------- read-only passthrough
    def connect(self) -> bool:
        self._broker.connect()
        return True

    def disconnect(self) -> None:
        self._broker.disconnect()

    def health(self) -> Dict[str, Any]:
        try:
            info = self._broker.account_info()
        except Exception as exc:  # pragma: no cover - defensive
            return {"status": "DISCONNECTED", "connected": False, "reason": redact(exc)}
        return {
            "status": "HEALTHY" if info.connected else "DISCONNECTED",
            "connected": info.connected,
            "trade_allowed": info.trade_allowed,
            "server": info.server,
        }

    def get_server_time(self) -> datetime:
        return datetime.now(timezone.utc)

    def get_account_snapshot(self) -> AccountSnapshot:
        info = self._broker.account_info()
        return AccountSnapshot(
            balance=info.balance,
            equity=info.equity,
            margin=info.margin,
            free_margin=info.margin_free,
        )

    def get_symbol_spec(self, symbol: str) -> SymbolSpec:
        info = self._broker.symbol_info(symbol)
        return SymbolSpec(
            symbol=info.provider_symbol,
            tick_size=info.trade_tick_size,
            tick_value=info.trade_tick_value,
            contract_size=info.contract_size,
            volume_min=info.volume_min,
            volume_max=info.volume_max,
            volume_step=info.volume_step,
            digits=info.digits,
        )

    def get_symbol_price(self, symbol: str) -> tuple:
        tick = self._broker.tick(symbol)
        info = self._broker.symbol_info(symbol)
        pip = info.pip_size
        spread_pips = (tick.ask - tick.bid) / pip if pip > 0 else 0.0
        return tick.bid, tick.ask, spread_pips

    def get_positions(self, symbol: Optional[str] = None) -> List[LegacyPositionInfo]:
        out = []
        for pos in self._broker.positions(symbol):
            out.append(
                LegacyPositionInfo(
                    position_id=pos.position_id,
                    client_order_id=pos.comment or None,
                    symbol=pos.symbol,
                    action=pos.side,
                    volume=pos.volume,
                    entry_price=pos.entry_price,
                    sl=pos.sl,
                    tp=pos.tp,
                    current_price=pos.current_price,
                    profit=pos.profit,
                    swap=pos.swap,
                    magic=pos.magic,
                    open_time=pos.open_time_utc,
                )
            )
        return out

    def get_open_positions_count(self, symbol: Optional[str] = None) -> int:
        return len(self._broker.positions(symbol))

    # ------------------------------------------------------ write passthrough
    def execute_order(
        self,
        symbol: str,
        action: SignalAction,
        lot_size: float,
        sl: float,
        tp: float,
        client_order_id: Optional[str] = None,
        comment: str = "PropBot Auto",
    ) -> LegacyOrderResult:
        intent = OrderIntent(
            symbol=symbol,
            side=OrderSide.BUY if action == SignalAction.BUY else OrderSide.SELL,
            volume=lot_size,
            sl=sl,
            tp=tp,
            client_order_id=client_order_id,
            comment=comment,
            magic=self._magic,
        )
        result = self._broker.place_order(intent)
        if client_order_id:
            self._fill_cache[client_order_id] = result
        if result.order_id:
            self._fill_cache[result.order_id] = result

        return LegacyOrderResult(
            success=result.success,
            order_id=result.order_id,
            client_order_id=result.client_order_id,
            symbol=result.symbol,
            action=result.side,
            lot_size=result.volume,
            price=result.filled_price,
            sl=result.sl,
            tp=result.tp,
            status=result.status.value,
            error_message=result.error_message,
            timestamp_utc=result.timestamp_utc,
        )

    def get_order(
        self,
        client_order_id: Optional[str] = None,
        broker_order_id: Optional[str] = None,
    ) -> Optional[LegacyOrderResult]:
        found = None
        if client_order_id:
            found = self._fill_cache.get(client_order_id)
        if found is None and broker_order_id:
            found = self._fill_cache.get(broker_order_id)
        if found is None:
            return None
        return LegacyOrderResult(
            success=found.success,
            order_id=found.order_id,
            client_order_id=found.client_order_id,
            symbol=found.symbol,
            action=found.side,
            lot_size=found.volume,
            price=found.filled_price,
            sl=found.sl,
            tp=found.tp,
            status=found.status.value,
            error_message=found.error_message,
            timestamp_utc=found.timestamp_utc,
        )

    def cancel_order(self, order_id: str) -> bool:
        cancel = getattr(self._broker, "cancel_order", None)
        if cancel is None:
            raise NotImplementedError("Wrapped broker does not support order cancellation")
        return bool(cancel(order_id))

    def close_position(self, position_id: str) -> bool:
        result = self._broker.close_position(position_id)
        return bool(result.success)

    def close_all_positions(self) -> int:
        closed = 0
        for pos in self._broker.positions():
            if self.close_position(pos.position_id):
                closed += 1
        return closed


class LegacyExecutionAdapter:
    """Adapts an existing BaseBrokerAdapter INTO the ExecutionBroker protocol.

    Used for the Mock adapter and the pre-existing MT5 adapter, so callers that
    depend only on the modern protocol keep working during migration. The legacy
    adapter is not deleted or changed; it is wrapped.
    """

    def __init__(self, legacy: BaseBrokerAdapter, canonical_symbols: Optional[List[str]] = None) -> None:
        self._legacy = legacy
        self._canonical_symbols = canonical_symbols
        self._provider_map: Dict[str, str] = {}

    @property
    def wrapped(self) -> BaseBrokerAdapter:
        return self._legacy

    def connect(self) -> None:
        if not self._legacy.connect():
            from src.execution.errors import ConnectionLostError

            raise ConnectionLostError("Legacy broker adapter refused to connect.")

    def disconnect(self) -> None:
        self._legacy.disconnect()

    def account_info(self) -> AccountInfo:
        snap = self._legacy.get_account_snapshot()
        health = self._legacy.health()
        return AccountInfo(
            login=0,
            server="legacy",
            balance=snap.balance,
            equity=snap.equity,
            margin=snap.margin,
            margin_free=snap.free_margin,
            trade_allowed=bool(health.get("connected")),
            connected=bool(health.get("connected")),
        )

    def _resolve(self, symbol: str) -> str:
        if symbol in self._provider_map:
            return self._provider_map[symbol]
        if hasattr(self._legacy, "list_symbols"):
            from src.execution.symbol_mapper import resolve_symbol

            resolution = resolve_symbol(symbol, list(self._legacy.list_symbols()))
            self._provider_map[symbol] = resolution.provider_symbol
            return resolution.provider_symbol
        return symbol

    def symbol_info(self, symbol: str) -> SymbolInfo:
        provider = self._resolve(symbol)
        spec = self._legacy.get_symbol_spec(provider)
        return _symbol_info_from_legacy(symbol, provider, spec)

    def tick(self, symbol: str) -> Tick:
        provider = self._resolve(symbol)
        bid, ask, _ = self._legacy.get_symbol_price(provider)
        now = datetime.now(timezone.utc)
        return Tick(
            canonical_symbol=symbol,
            provider_symbol=provider,
            bid=bid,
            ask=ask,
            timestamp_utc=now,
            local_receipt_utc=now,
        )

    def positions(self, symbol: Optional[str] = None) -> List[PositionSnapshot]:
        provider = self._resolve(symbol) if symbol else None
        out = []
        for pos in self._legacy.get_positions(provider):
            out.append(
                PositionSnapshot(
                    position_id=pos.position_id,
                    symbol=symbol or pos.symbol,
                    side=pos.action,
                    volume=pos.volume,
                    entry_price=pos.entry_price,
                    current_price=pos.current_price,
                    sl=pos.sl,
                    tp=pos.tp,
                    profit=pos.profit,
                    swap=pos.swap,
                    magic=pos.magic,
                    comment=pos.client_order_id or "",
                    open_time_utc=pos.open_time,
                )
            )
        return out

    def orders(self, symbol: Optional[str] = None) -> List[PendingOrderSnapshot]:
        return []

    def place_order(self, request: OrderIntent) -> OrderResultInfo:
        provider = self._resolve(request.symbol)
        legacy_result = self._legacy.execute_order(
            symbol=provider,
            action=SignalAction.BUY if request.side == OrderSide.BUY else SignalAction.SELL,
            lot_size=request.volume,
            sl=request.sl,
            tp=request.tp,
            client_order_id=request.client_order_id,
            comment=request.comment,
        )
        from src.execution.models import OrderStatus

        return OrderResultInfo(
            success=legacy_result.success,
            order_id=legacy_result.order_id,
            client_order_id=legacy_result.client_order_id,
            symbol=request.symbol,
            side=request.side.value,
            volume=legacy_result.lot_size,
            requested_price=legacy_result.price,
            filled_price=legacy_result.price,
            sl=legacy_result.sl,
            tp=legacy_result.tp,
            status=OrderStatus.FILLED if legacy_result.success else OrderStatus.REJECTED,
            error_message=legacy_result.error_message,
            timestamp_utc=legacy_result.timestamp_utc,
        )

    def close_position(self, position_id: str, reason: str = "") -> OrderResultInfo:
        from src.execution.models import OrderStatus

        ok = self._legacy.close_position(position_id)
        return OrderResultInfo(
            success=bool(ok),
            order_id=position_id,
            client_order_id=None,
            symbol="",
            side="CLOSE",
            volume=0.0,
            requested_price=0.0,
            filled_price=0.0,
            status=OrderStatus.FILLED if ok else OrderStatus.REJECTED,
            timestamp_utc=datetime.now(timezone.utc),
        )

    def cancel_order(self, order_id: str) -> bool:
        return bool(self._legacy.cancel_order(order_id))


def _symbol_info_from_legacy(canonical: str, provider: str, spec: SymbolSpec) -> SymbolInfo:
    """Translate a legacy SymbolSpec into the modern SymbolInfo."""
    from src.execution.models import TradeMode

    return SymbolInfo(
        canonical_symbol=canonical,
        provider_symbol=provider,
        digits=spec.digits,
        point=spec.tick_size,
        trade_tick_size=spec.tick_size,
        trade_tick_value=spec.tick_value,
        contract_size=spec.contract_size,
        volume_min=spec.volume_min,
        volume_max=spec.volume_max,
        volume_step=spec.volume_step,
        trade_mode=TradeMode.FULL,
        currency_base=canonical[:3] if len(canonical) >= 6 else "",
        currency_profit=canonical[3:6] if len(canonical) >= 6 else "",
        currency_margin="USD",
        spread=0,
    )


# ---------------------------------------------------------------------- factory
@dataclass
class ExecutionProvider:
    """Identifiers for the selectable execution providers."""

    FUNDINGPIPS_MT5: str = "fundingpips_mt5"
    LEGACY_MT5: str = "mt5_legacy"
    MOCK: str = "mock"


ENV_EXECUTION_PROVIDER = "EXECUTION_PROVIDER"
DEFAULT_PROVIDER = ExecutionProvider.FUNDINGPIPS_MT5


def resolve_provider(env: Optional[Dict[str, str]] = None) -> str:
    """Read EXECUTION_PROVIDER, defaulting to FundingPips MT5."""
    import os

    source = os.environ if env is None else env
    raw = (source.get(ENV_EXECUTION_PROVIDER) or "").strip().lower()
    if not raw:
        return DEFAULT_PROVIDER
    aliases = {"fundingpips": ExecutionProvider.FUNDINGPIPS_MT5,
               "mt5": ExecutionProvider.LEGACY_MT5}
    return aliases.get(raw, raw)


def get_execution_broker(env: Optional[Dict[str, str]] = None, **kwargs: Any) -> ExecutionBroker:
    """Dependency-injection point for the execution adapter.

    This is the ONLY place that decides which concrete adapter exists, so the
    repository never needs scattered "if fundingpips:" branches.
    """
    provider = resolve_provider(env)
    if provider == ExecutionProvider.FUNDINGPIPS_MT5:
        from src.execution.fundingpips_mt5 import FundingPipsMT5Adapter

        return FundingPipsMT5Adapter.from_env(env=env, **kwargs)
    if provider == ExecutionProvider.MOCK:
        from src.execution.broker_adapter import MockBrokerAdapter

        return LegacyExecutionAdapter(MockBrokerAdapter(**kwargs))
    raise ValueError(
        "Unknown execution provider %r. Supported: %s, %s, %s"
        % (
            redact(provider),
            ExecutionProvider.FUNDINGPIPS_MT5,
            ExecutionProvider.LEGACY_MT5,
            ExecutionProvider.MOCK,
        )
    )

