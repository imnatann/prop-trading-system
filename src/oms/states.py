"""
OMS Order States & Event Types.
Mendefinisikan Finite State Machine (FSM) untuk siklus hidup order prop trading.
"""

from enum import Enum


class OrderState(str, Enum):
    """Status siklus hidup order dalam OMS."""
    CREATED = "CREATED"                     # Order baru dibuat di memori
    RISK_APPROVED = "RISK_APPROVED"         # Lolos validasi Pre-Trade Risk Gatekeeper
    SUBMITTING = "SUBMITTING"               # Sedang dipersiapkan untuk dikirim
    SENT = "SENT"                           # Payload order telah terkirim ke broker
    ACKNOWLEDGED = "ACKNOWLEDGED"           # Broker mengonfirmasi order diterima
    PARTIALLY_FILLED = "PARTIALLY_FILLED"   # Terisi sebagian
    FILLED = "FILLED"                       # Terisi penuh (Terminal State)
    CANCEL_PENDING = "CANCEL_PENDING"       # Permintaan pembatalan terkirim
    CANCELLED = "CANCELLED"                 # Dibatalkan oleh user/broker (Terminal State)
    REJECTED = "REJECTED"                   # Ditolak oleh risk/broker (Terminal State)
    UNKNOWN = "UNKNOWN"                     # Status tidak pasti akibat timeout/koneksi putus
    EXPIRED = "EXPIRED"                     # Kadaluarsa (Terminal State)


class OrderEventType(str, Enum):
    """Tipe peristiwa mutasi status order untuk audit trail."""
    ORDER_CREATED = "ORDER_CREATED"
    RISK_APPROVED = "RISK_APPROVED"
    RISK_REJECTED = "RISK_REJECTED"
    BROKER_SUBMIT = "BROKER_SUBMIT"
    BROKER_ACK = "BROKER_ACK"
    PARTIAL_FILL = "PARTIAL_FILL"
    FILL = "FILL"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    TIMEOUT = "TIMEOUT"
    UNKNOWN_RECOVERY = "UNKNOWN_RECOVERY"
