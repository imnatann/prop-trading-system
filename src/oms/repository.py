"""
OMS SQLite WAL Authoritative Repository.
Menyimpan state order, event audit log, fill, dan posisi secara tahan banting (durable & transactional).
Menggunakan PRAGMA journal_mode=WAL dan PRAGMA synchronous=FULL.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from loguru import logger

from src.oms.models import Order, OrderEvent, Fill, PositionRecord
from src.oms.states import OrderState, OrderEventType


class OMSRepository:
    """Authoritative Local Database untuk Order Management System."""

    def __init__(self, db_path: str = "storage/trading.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        # Pengaturan WAL mode dan durabilitas ACID transaksi
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=FULL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_db(self) -> None:
        """Membuat tabel skema database OMS jika belum ada."""
        with self._get_connection() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                client_order_id TEXT UNIQUE NOT NULL,
                symbol TEXT NOT NULL,
                action TEXT NOT NULL,
                requested_lot REAL NOT NULL,
                filled_lot REAL DEFAULT 0.0,
                limit_price REAL,
                sl_price REAL DEFAULT 0.0,
                tp_price REAL DEFAULT 0.0,
                broker_order_id TEXT,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS order_events (
                event_id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL,
                client_order_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                timestamp_utc TEXT NOT NULL,
                payload TEXT,
                FOREIGN KEY (order_id) REFERENCES orders(order_id)
            );

            CREATE TABLE IF NOT EXISTS fills (
                fill_id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                volume REAL NOT NULL,
                price REAL NOT NULL,
                commission REAL DEFAULT 0.0,
                swap REAL DEFAULT 0.0,
                timestamp_utc TEXT NOT NULL,
                FOREIGN KEY (order_id) REFERENCES orders(order_id)
            );

            CREATE TABLE IF NOT EXISTS positions (
                position_id TEXT PRIMARY KEY,
                client_order_id TEXT,
                symbol TEXT NOT NULL,
                action TEXT NOT NULL,
                volume REAL NOT NULL,
                entry_price REAL NOT NULL,
                sl REAL DEFAULT 0.0,
                tp REAL DEFAULT 0.0,
                current_price REAL DEFAULT 0.0,
                unrealized_pnl REAL DEFAULT 0.0,
                status TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                closed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS system_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                message TEXT NOT NULL,
                timestamp_utc TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_orders_client_id ON orders(client_order_id);
            CREATE INDEX IF NOT EXISTS idx_orders_state ON orders(state);
            CREATE INDEX IF NOT EXISTS idx_events_order_id ON order_events(order_id);
            CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
            """)
            conn.commit()

    # --- ORDER OPERATIONS ---

    def save_order(self, order: Order) -> None:
        """Menyimpan atau memperbarui order (Atomic)."""
        now_str = order.updated_at.isoformat()
        with self._get_connection() as conn:
            conn.execute("""
            INSERT INTO orders (
                order_id, client_order_id, symbol, action, requested_lot,
                filled_lot, limit_price, sl_price, tp_price, broker_order_id,
                state, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(order_id) DO UPDATE SET
                filled_lot=excluded.filled_lot,
                broker_order_id=excluded.broker_order_id,
                state=excluded.state,
                updated_at=excluded.updated_at;
            """, (
                order.order_id, order.client_order_id, order.symbol, order.action,
                order.requested_lot, order.filled_lot, order.limit_price, order.sl_price,
                order.tp_price, order.broker_order_id, order.state.value,
                order.created_at.isoformat(), now_str
            ))
            conn.commit()

    def get_order(self, order_id: str) -> Optional[Order]:
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return self._row_to_order(row)

    def get_order_by_client_id(self, client_order_id: str) -> Optional[Order]:
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return self._row_to_order(row)

    def get_active_orders(self) -> List[Order]:
        """Mengambil order yang belum masuk terminal state."""
        terminal_states = (
            OrderState.FILLED.value,
            OrderState.CANCELLED.value,
            OrderState.REJECTED.value,
            OrderState.EXPIRED.value
        )
        query = f"SELECT * FROM orders WHERE state NOT IN ({','.join(['?']*len(terminal_states))})"
        with self._get_connection() as conn:
            cursor = conn.execute(query, terminal_states)
            return [self._row_to_order(r) for r in cursor.fetchall()]

    def _row_to_order(self, row: sqlite3.Row) -> Order:
        return Order(
            order_id=row["order_id"],
            client_order_id=row["client_order_id"],
            symbol=row["symbol"],
            action=row["action"],
            requested_lot=row["requested_lot"],
            filled_lot=row["filled_lot"],
            limit_price=row["limit_price"],
            sl_price=row["sl_price"],
            tp_price=row["tp_price"],
            broker_order_id=row["broker_order_id"],
            state=OrderState(row["state"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"])
        )

    # --- EVENT OPERATIONS ---

    def record_event(self, event: OrderEvent) -> None:
        """Mencatat mutasi status order ke event log (Append-only)."""
        with self._get_connection() as conn:
            conn.execute("""
            INSERT INTO order_events (
                event_id, order_id, client_order_id, event_type,
                from_state, to_state, timestamp_utc, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                event.event_id, event.order_id, event.client_order_id,
                event.event_type.value, event.from_state.value, event.to_state.value,
                event.timestamp_utc.isoformat(), json.dumps(event.payload)
            ))
            conn.commit()

    def get_order_events(self, order_id: str) -> List[OrderEvent]:
        with self._get_connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM order_events WHERE order_id = ? ORDER BY timestamp_utc ASC",
                (order_id,)
            )
            events = []
            for r in cursor.fetchall():
                events.append(OrderEvent(
                    event_id=r["event_id"],
                    order_id=r["order_id"],
                    client_order_id=r["client_order_id"],
                    event_type=OrderEventType(r["event_type"]),
                    from_state=OrderState(r["from_state"]),
                    to_state=OrderState(r["to_state"]),
                    timestamp_utc=datetime.fromisoformat(r["timestamp_utc"]),
                    payload=json.loads(r["payload"]) if r["payload"] else {}
                ))
            return events

    # --- POSITION OPERATIONS ---

    def upsert_position(self, pos: PositionRecord) -> None:
        with self._get_connection() as conn:
            conn.execute("""
            INSERT INTO positions (
                position_id, client_order_id, symbol, action, volume,
                entry_price, sl, tp, current_price, unrealized_pnl,
                status, opened_at, closed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(position_id) DO UPDATE SET
                current_price=excluded.current_price,
                unrealized_pnl=excluded.unrealized_pnl,
                status=excluded.status,
                closed_at=excluded.closed_at;
            """, (
                pos.position_id, pos.client_order_id, pos.symbol, pos.action,
                pos.volume, pos.entry_price, pos.sl, pos.tp,
                pos.current_price, pos.unrealized_pnl, pos.status,
                pos.opened_at.isoformat(),
                pos.closed_at.isoformat() if pos.closed_at else None
            ))
            conn.commit()

    def get_open_positions(self) -> List[PositionRecord]:
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM positions WHERE status = 'OPEN'")
            return [self._row_to_position(r) for r in cursor.fetchall()]

    def _row_to_position(self, row: sqlite3.Row) -> PositionRecord:
        return PositionRecord(
            position_id=row["position_id"],
            client_order_id=row["client_order_id"],
            symbol=row["symbol"],
            action=row["action"],
            volume=row["volume"],
            entry_price=row["entry_price"],
            sl=row["sl"],
            tp=row["tp"],
            current_price=row["current_price"],
            unrealized_pnl=row["unrealized_pnl"],
            status=row["status"],
            opened_at=datetime.fromisoformat(row["opened_at"]),
            closed_at=datetime.fromisoformat(row["closed_at"]) if row["closed_at"] else None
        )
