"""
Idempotency Management for Order Execution.
Mencegah duplikasi order akibat retry agresif, network jitter, atau double-firing sinyal.
"""

import threading
import uuid
from datetime import datetime, timezone
from typing import Optional, Set
from loguru import logger


def generate_client_order_id(prop_prefix: str, symbol: str) -> str:
    """
    Menghasilkan Client Order ID yang unik, deterministik, dan dapat dilacak.
    Format: QP-<PROP>-<YYYYMMDD>-<SYMBOL>-<HEX8>
    Contoh: QP-FP-20260920-EURUSD-a1b2c3d4
    """
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y%m%d")
    clean_sym = symbol.replace("/", "").replace(".", "").upper()
    token = uuid.uuid4().hex[:8]
    return f"QP-{prop_prefix.upper()}-{date_str}-{clean_sym}-{token}"


class IdempotencyRegistry:
    """
    Registry thread-safe untuk mengontrol in-flight dan executed client_order_id.
    Menolak submission ganda jika ID yang sama sedang diproses atau telah selesai.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._in_flight: Set[str] = set()
        self._completed: Set[str] = set()

    def acquire(self, client_order_id: str) -> bool:
        """
        Mencoba mengunci client_order_id.
        Mengembalikan True jika berhasil dikunci (order baru).
        Mengembalikan False jika ID sudah in-flight atau sudah completed (duplicate).
        """
        with self._lock:
            if client_order_id in self._in_flight or client_order_id in self._completed:
                logger.warning(f"IdempotencyRegistry: Duplicate submission rejected for {client_order_id}")
                return False
            self._in_flight.add(client_order_id)
            return True

    def release_success(self, client_order_id: str) -> None:
        """Menandai order selesai berhasil."""
        with self._lock:
            self._in_flight.discard(client_order_id)
            self._completed.add(client_order_id)

    def release_failure(self, client_order_id: str) -> None:
        """Menandai order gagal dieksekusi sehingga ID dilepas dari in-flight."""
        with self._lock:
            self._in_flight.discard(client_order_id)
            self._completed.add(client_order_id)

    def is_known(self, client_order_id: str) -> bool:
        with self._lock:
            return client_order_id in self._in_flight or client_order_id in self._completed
