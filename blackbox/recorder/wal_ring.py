"""
Genesis Blackbox: Circular SQLite WAL Ring-Buffer
High-throughput, sub-millisecond execution flight recorder.
Stores application events, HTTP transactions, SQL queries, and runtime state mutations.
"""

import os
import time
import json
import sqlite3
import threading
from typing import Dict, Any, List, Optional


class WalRingBuffer:
    """
    Circular ring-buffer backed by a SQLite database in WAL mode.
    Guarantees sub-millisecond event ingestion with automatic circular eviction.
    """

    def __init__(
        self,
        db_path: str = ":memory:",
        max_records: int = 5000,
        prune_batch_size: int = 500
    ):
        self.db_path = db_path
        self.max_records = max(10, max_records)
        self.prune_batch_size = max(5, prune_batch_size)
        self._lock = threading.Lock()
        self._insert_counter = 0

        # Initialize directory if persistent database
        if self.db_path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)

        self._conn = self._create_connection()
        self._init_schema()

    def _create_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10.0)
        conn.row_factory = sqlite3.Row
        # Configure high-speed WAL pragma settings
        if self.db_path != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA temp_store=MEMORY;")
        conn.execute("PRAGMA cache_size=-4000;")  # 4MB cache
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            with self._conn:
                self._conn.execute("""
                    CREATE TABLE IF NOT EXISTS blackbox_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        event_type TEXT NOT NULL,
                        source TEXT NOT NULL,
                        duration_ms REAL,
                        trace_id TEXT,
                        span_id TEXT,
                        status TEXT NOT NULL,
                        payload TEXT NOT NULL
                    );
                """)
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_bb_timestamp ON blackbox_events(timestamp);")
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_bb_event_type ON blackbox_events(event_type);")
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_bb_trace_id ON blackbox_events(trace_id);")

    def record(
        self,
        event_type: str,
        payload: Any,
        source: str = "app",
        duration_ms: Optional[float] = None,
        trace_id: Optional[str] = None,
        span_id: Optional[str] = None,
        status: str = "ok"
    ) -> int:
        """
        Records an execution event into the WAL ring-buffer.
        Serialized payload safely handles dict, list, or primitive types.
        """
        ts = time.time()
        payload_json = json.dumps(payload, default=str)

        with self._lock:
            with self._conn:
                cursor = self._conn.execute(
                    """
                    INSERT INTO blackbox_events (
                        timestamp, event_type, source, duration_ms, trace_id, span_id, status, payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (ts, event_type, source, duration_ms, trace_id, span_id, status, payload_json)
                )
                event_id = cursor.lastrowid
                self._insert_counter += 1

                # Prune in batches to minimize write latency
                if self._insert_counter >= self.prune_batch_size:
                    self._prune_unlocked()
                    self._insert_counter = 0

            return event_id

    def _prune_unlocked(self) -> None:
        """Removes older records when table size exceeds max_records."""
        cursor = self._conn.execute("SELECT COUNT(*) FROM blackbox_events;")
        total_records = cursor.fetchone()[0]

        if total_records > self.max_records:
            excess = total_records - self.max_records
            # Delete in chunks
            self._conn.execute(
                """
                DELETE FROM blackbox_events
                WHERE id IN (
                    SELECT id FROM blackbox_events
                    ORDER BY id ASC
                    LIMIT ?
                );
                """,
                (excess + self.prune_batch_size,)
            )

    def prune_if_needed(self) -> None:
        """Explicitly forces pruning if over capacity."""
        with self._lock:
            with self._conn:
                self._prune_unlocked()

    def query_recent(
        self,
        limit: int = 50,
        event_type: Optional[str] = None,
        trace_id: Optional[str] = None,
        source: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Queries the most recent events, sorted descending by timestamp."""
        query = "SELECT * FROM blackbox_events WHERE 1=1"
        params: List[Any] = []

        if event_type:
            query += " AND event_type = ?"
            params.append(event_type)
        if trace_id:
            query += " AND trace_id = ?"
            params.append(trace_id)
        if source:
            query += " AND source = ?"
            params.append(source)

        query += " ORDER BY id DESC LIMIT ?;"
        params.append(limit)

        with self._lock:
            cursor = self._conn.execute(query, params)
            rows = cursor.fetchall()

        events = []
        for r in rows:
            events.append(self._row_to_dict(r))
        return events

    def get_event(self, event_id: int) -> Optional[Dict[str, Any]]:
        """Retrieves a single event by ID."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM blackbox_events WHERE id = ?;", (event_id,)
            )
            row = cursor.fetchone()

        if row:
            return self._row_to_dict(row)
        return None

    def get_timeline(
        self,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
        limit: int = 200
    ) -> List[Dict[str, Any]]:
        """Retrieves chronological execution timeline for time-travel analysis."""
        query = "SELECT * FROM blackbox_events WHERE 1=1"
        params: List[Any] = []

        if start_time is not None:
            query += " AND timestamp >= ?"
            params.append(start_time)
        if end_time is not None:
            query += " AND timestamp <= ?"
            params.append(end_time)

        query += " ORDER BY id ASC LIMIT ?;"
        params.append(limit)

        with self._lock:
            cursor = self._conn.execute(query, params)
            rows = cursor.fetchall()

        return [self._row_to_dict(r) for r in rows]

    def count(self) -> int:
        """Returns total active events in the ring-buffer."""
        with self._lock:
            cursor = self._conn.execute("SELECT COUNT(*) FROM blackbox_events;")
            return cursor.fetchone()[0]

    def clear(self) -> None:
        """Empties the ring buffer."""
        with self._lock:
            with self._conn:
                self._conn.execute("DELETE FROM blackbox_events;")
                self._insert_counter = 0

    def close(self) -> None:
        """Closes the underlying database connection."""
        with self._lock:
            self._conn.close()

    def _row_to_dict(self, row: sqlite3.Row) -> Dict[str, Any]:
        data = dict(row)
        try:
            data["payload"] = json.loads(data["payload"])
        except Exception:
            pass
        return data
