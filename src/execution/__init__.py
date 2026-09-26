"""
Execution layer public surface.

Two tiers:
  * Legacy (unchanged): BrokerAdapter / MockBrokerAdapter / MT5BrokerAdapter / OrderManager
    / OrderDispatcher. Kept intact so existing tests and the OMS stack keep working.
  * Modern execution protocol: ExecutionBroker + FundingPipsMT5Adapter, selected via
    EXECUTION_PROVIDER and constructed only in src.execution.base.get_execution_broker().

Notably NOT exported here: MetaTrader5. Importing this package must never require the
MT5 runtime, because research and CI run on macOS/Linux.
"""
from .broker_adapter import BrokerAdapter, MockBrokerAdapter, MT5BrokerAdapter, OrderResult
from .order_manager import OrderManager
from .idempotency import IdempotencyRegistry, generate_client_order_id
from .dispatcher import OrderDispatcher

from .base import (
    BrokerExecutionAdapter,
    ENV_EXECUTION_PROVIDER,
    ExecutionBroker,
    ExecutionProvider,
    LegacyExecutionAdapter,
    get_execution_broker,
    resolve_provider,
)
from .errors import (
    AccountMismatchError,
    AmbiguousSymbolError,
    ConnectionLostError,
    CredentialFormatError,
    DuplicateOrderError,
    ExecutionError,
    MissingCredentialError,
    MT5AuthenticationError,
    MT5InitializationError,
    MT5UnavailableError,
    OrderPreflightError,
    SymbolNotFoundError,
    TradingDisabledError,
    UnexpectedServerError,
    UnsupportedExecutionEnvironmentError,
)
from .execution_profile import FundingPipsExecutionProfile, SpreadStats, build_profile
from .models import (
    AccountInfo,
    OrderIntent,
    OrderResultInfo,
    OrderSide,
    OrderStatus,
    PendingOrderSnapshot,
    PositionSnapshot,
    SimulationCostParameters,
    SymbolInfo,
    Tick,
    TradeMode,
)
from .redaction import MASK, Secret, mask_account, redact, redact_mapping, register_secret, scrub
from .symbol_mapper import SymbolResolution, resolve_symbol
from .telemetry import (
    CANONICAL_DIR,
    RAW_DIR,
    CostBreakdown,
    SpreadRecorder,
    SpreadSample,
    attribute_round_trip,
    exec_event,
)

__all__ = [
    # legacy
    "BrokerAdapter", "MockBrokerAdapter", "MT5BrokerAdapter", "OrderResult",
    "OrderManager", "IdempotencyRegistry", "generate_client_order_id", "OrderDispatcher",
    # protocol + factories
    "ExecutionBroker", "ExecutionProvider", "ENV_EXECUTION_PROVIDER",
    "get_execution_broker", "resolve_provider",
    "BrokerExecutionAdapter", "LegacyExecutionAdapter",
    # errors
    "ExecutionError", "MT5UnavailableError", "UnsupportedExecutionEnvironmentError",
    "MissingCredentialError", "CredentialFormatError", "MT5InitializationError",
    "MT5AuthenticationError", "UnexpectedServerError", "AccountMismatchError",
    "SymbolNotFoundError", "AmbiguousSymbolError", "TradingDisabledError",
    "OrderPreflightError", "ConnectionLostError", "DuplicateOrderError",
    # models
    "AccountInfo", "SymbolInfo", "Tick", "OrderIntent", "OrderResultInfo",
    "OrderSide", "OrderStatus", "PositionSnapshot", "PendingOrderSnapshot",
    "TradeMode", "SimulationCostParameters",
    # profile / calibration
    "FundingPipsExecutionProfile", "SpreadStats", "build_profile",
    # redaction
    "Secret", "redact", "scrub", "redact_mapping", "mask_account", "register_secret", "MASK",
    # symbols / telemetry
    "resolve_symbol", "SymbolResolution",
    "SpreadRecorder", "SpreadSample", "CostBreakdown", "attribute_round_trip",
    "exec_event", "RAW_DIR", "CANONICAL_DIR",
]
