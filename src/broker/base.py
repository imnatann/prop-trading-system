"""
Broker Adapter Base Interface.
Mendefinisikan kontrak lengkap (Capability-Complete Interface) untuk semua konektor broker:
Mock, MT5 (Windows VPS), dan cTrader (Linux).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from src.broker.models import SymbolSpec
from src.risk.drawdown_monitor import AccountSnapshot
from src.strategy.base import SignalAction


@dataclass
class OrderResult:
    success: bool
    order_id: Optional[str]           # Broker ticket / ID
    client_order_id: Optional[str]    # Unique client order ID
    symbol: str
    action: str
    lot_size: float
    price: float
    sl: float
    tp: float
    status: str = "FILLED"            # FILLED, REJECTED, SUBMITTED
    error_message: Optional[str] = None
    timestamp_utc: Optional[datetime] = None


@dataclass
class PositionInfo:
    position_id: str
    client_order_id: Optional[str]
    symbol: str
    action: str                       # BUY or SELL
    volume: float
    entry_price: float
    sl: float
    tp: float
    current_price: float
    profit: float
    swap: float = 0.0
    magic: int = 888999
    open_time: Optional[datetime] = None


class BaseBrokerAdapter(ABC):
    """Kontrak lengkap abstraksi broker institusional."""

    @abstractmethod
    def connect(self) -> bool:
        """Koneksi ke server broker."""
        pass

    @abstractmethod
    def disconnect(self) -> None:
        """Putus koneksi dari server broker."""
        pass

    @abstractmethod
    def health(self) -> Dict[str, Any]:
        """Cek status kesehatan koneksi dan latensi RTT broker."""
        pass

    @abstractmethod
    def get_server_time(self) -> datetime:
        """Mengambil waktu server broker saat ini."""
        pass

    @abstractmethod
    def get_account_snapshot(self) -> AccountSnapshot:
        """Mengambil saldo, equity, margin, dan free margin terkini."""
        pass

    @abstractmethod
    def get_symbol_spec(self, symbol: str) -> SymbolSpec:
        """Mengambil metadata spesifikasi broker-native untuk instrumen."""
        pass

    @abstractmethod
    def get_symbol_price(self, symbol: str) -> Tuple[float, float, float]:
        """Returns: (bid, ask, spread_pips)"""
        pass

    @abstractmethod
    def get_positions(self, symbol: Optional[str] = None) -> List[PositionInfo]:
        """Mengambil daftar seluruh posisi terbuka aktual di server broker."""
        pass

    @abstractmethod
    def get_open_positions_count(self, symbol: Optional[str] = None) -> int:
        """Mengambil jumlah posisi terbuka."""
        pass

    @abstractmethod
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
        """Kirim order atomik dengan SL dan TP terpasang."""
        pass

    @abstractmethod
    def get_order(
        self,
        client_order_id: Optional[str] = None,
        broker_order_id: Optional[str] = None
    ) -> Optional[OrderResult]:
        """Query order ke server broker berdasarkan ticket atau client_order_id."""
        pass

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """Batalkan pending order."""
        pass

    @abstractmethod
    def close_position(self, position_id: str) -> bool:
        """Tutup satu posisi spesifik di broker."""
        pass

    @abstractmethod
    def close_all_positions(self) -> int:
        """Likuidasi darurat seluruh posisi terbuka (Emergency Liquidation)."""
        pass
