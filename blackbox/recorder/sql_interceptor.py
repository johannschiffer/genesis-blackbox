"""
Genesis Blackbox: SQL Execution Interceptor
Captures SQL queries, bound parameters (with automated credential redaction),
execution timings, affected rowcounts, and exact database exceptions.
Compatible with standard DB-API (sqlite3, psycopg2), asyncpg, and SQLAlchemy.
"""

import time
from typing import Any, Dict, List, Optional, Sequence, Union
from .wal_ring import WalRingBuffer

SENSITIVE_SQL_KEYS = {
    "password", "passwd", "secret", "token", "auth", "key",
    "api_key", "credential", "private", "access_token", "hash"
}


def _sanitize_value(val: Any) -> Any:
    if isinstance(val, (bytes, bytearray)):
        return f"<bytes: {len(val)} bytes>"
    if isinstance(val, str):
        if len(val) > 200:
            return val[:200] + "... (truncated)"
        return val
    if isinstance(val, (int, float, bool)) or val is None:
        return val
    return str(val)


def sanitize_sql_params(params: Any) -> Any:
    """Sanitizes bound SQL parameters, redacting passwords, tokens, and secrets."""
    if params is None:
        return None

    if isinstance(params, dict):
        sanitized = {}
        for k, v in params.items():
            k_lower = str(k).lower()
            if any(s in k_lower for s in SENSITIVE_SQL_KEYS):
                sanitized[k] = "[REDACTED]"
            elif isinstance(v, dict):
                sanitized[k] = sanitize_sql_params(v)
            else:
                sanitized[k] = _sanitize_value(v)
        return sanitized

    if isinstance(params, (list, tuple)):
        sanitized_list = []
        for item in params:
            if isinstance(item, dict):
                sanitized_list.append(sanitize_sql_params(item))
            elif isinstance(item, str) and any(s in item.lower() for s in ["password=", "token="]):
                sanitized_list.append("[REDACTED]")
            else:
                sanitized_list.append(_sanitize_value(item))
        return type(params)(sanitized_list)

    return _sanitize_value(params)


class SqlTraceContext:
    """Context manager for measuring and recording a single SQL execution."""

    def __init__(
        self,
        wal_ring: WalRingBuffer,
        query: str,
        params: Any = None,
        database: str = "sqlite"
    ):
        self.wal_ring = wal_ring
        self.query = query.strip()
        self.params = params
        self.database = database
        self.start_time = 0.0
        self.rows_affected = 0
        self.status = "ok"
        self.error: Optional[str] = None
        self.error_type: Optional[str] = None

    def __enter__(self):
        self.start_time = time.perf_counter()
        return self

    def set_rows_affected(self, count: int) -> None:
        self.rows_affected = count

    def __exit__(self, exc_type, exc_val, exc_tb):
        duration_ms = (time.perf_counter() - self.start_time) * 1000.0

        if exc_val is not None:
            self.status = "error"
            self.error = str(exc_val)
            self.error_type = exc_type.__name__ if exc_type else "Exception"

        payload = {
            "query": self.query,
            "params": sanitize_sql_params(self.params),
            "database": self.database,
            "rows_affected": self.rows_affected
        }
        if self.error:
            payload["error"] = self.error
            payload["error_type"] = self.error_type

        self.wal_ring.record(
            event_type="sql_query",
            source=self.database,
            duration_ms=round(duration_ms, 3),
            status=self.status,
            payload=payload
        )


class TracedCursor:
    """Wrapper around any DB-API cursor that intercepts execution calls."""

    def __init__(self, cursor: Any, wal_ring: WalRingBuffer, database: str = "sqlite"):
        self._cursor = cursor
        self._wal_ring = wal_ring
        self._database = database

    def execute(self, query: str, params: Any = None) -> Any:
        with SqlTraceContext(self._wal_ring, query, params, database=self._database) as tracer:
            result = self._cursor.execute(query, params) if params is not None else self._cursor.execute(query)
            tracer.set_rows_affected(getattr(self._cursor, "rowcount", 0))
            return result

    def executemany(self, query: str, seq_of_params: Sequence[Any]) -> Any:
        with SqlTraceContext(self._wal_ring, query, seq_of_params, database=self._database) as tracer:
            result = self._cursor.executemany(query, seq_of_params)
            tracer.set_rows_affected(getattr(self._cursor, "rowcount", 0))
            return result

    def fetchone(self) -> Any:
        return self._cursor.fetchone()

    def fetchall(self) -> List[Any]:
        return self._cursor.fetchall()

    def fetchmany(self, size: int = 0) -> List[Any]:
        return self._cursor.fetchmany(size) if size else self._cursor.fetchmany()

    def close(self) -> None:
        return self._cursor.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)

    def __iter__(self):
        return iter(self._cursor)


class TracedConnection:
    """Wrapper around a DB-API connection providing auto-traced cursors."""

    def __init__(self, conn: Any, wal_ring: WalRingBuffer, database: str = "sqlite3"):
        self._conn = conn
        self._wal_ring = wal_ring
        self._database = database

    def cursor(self, *args, **kwargs) -> TracedCursor:
        c = self._conn.cursor(*args, **kwargs)
        return TracedCursor(c, self._wal_ring, database=self._database)

    def execute(self, query: str, params: Any = None) -> Any:
        c = self.cursor()
        return c.execute(query, params)

    def executemany(self, query: str, seq_of_params: Sequence[Any]) -> Any:
        c = self.cursor()
        return c.executemany(query, seq_of_params)

    def commit(self) -> None:
        return self._conn.commit()

    def rollback(self) -> None:
        return self._conn.rollback()

    def close(self) -> None:
        return self._conn.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_val is None:
            self._conn.commit()
        else:
            self._conn.rollback()


class SqlInterceptor:
    """Manager for hooking databases and logging SQL queries into WalRingBuffer."""

    def __init__(self, wal_ring: WalRingBuffer):
        self.wal_ring = wal_ring

    def trace(self, query: str, params: Any = None, database: str = "sqlite") -> SqlTraceContext:
        """Returns a context manager for manual SQL query tracing."""
        return SqlTraceContext(self.wal_ring, query, params, database=database)

    def wrap_cursor(self, cursor: Any, database: str = "sqlite") -> TracedCursor:
        """Wraps a DB-API cursor with Blackbox tracing."""
        return TracedCursor(cursor, self.wal_ring, database=database)

    def hook_sqlite3_connection(self, conn: Any) -> TracedConnection:
        """
        Wraps a connection so all future cursors or executions from this connection
        are automatically traced into the WAL ring-buffer.
        """
        return TracedConnection(conn, self.wal_ring, database="sqlite3")

    def hook_sqlalchemy(self, engine: Any) -> None:
        """Hooks SQLAlchemy engine execution events if SQLAlchemy is present."""
        try:
            from sqlalchemy import event

            @event.listens_for(engine, "before_cursor_execute")
            def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
                conn.info["bb_sql_start"] = time.perf_counter()

            @event.listens_for(engine, "after_cursor_execute")
            def after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
                start = conn.info.pop("bb_sql_start", time.perf_counter())
                duration_ms = (time.perf_counter() - start) * 1000.0
                rowcount = getattr(cursor, "rowcount", 0)

                self.wal_ring.record(
                    event_type="sql_query",
                    source="sqlalchemy",
                    duration_ms=round(duration_ms, 3),
                    status="ok",
                    payload={
                        "query": statement.strip(),
                        "params": sanitize_sql_params(parameters),
                        "database": getattr(engine.dialect, "name", "sql"),
                        "rows_affected": rowcount
                    }
                )

            @event.listens_for(engine, "handle_error")
            def handle_error(exception_context):
                conn_obj = getattr(exception_context, "connection", None)
                start = conn_obj.info.pop("bb_sql_start", time.perf_counter()) if conn_obj is not None else time.perf_counter()
                duration_ms = (time.perf_counter() - start) * 1000.0

                self.wal_ring.record(
                    event_type="sql_query",
                    source="sqlalchemy",
                    duration_ms=round(duration_ms, 3),
                    status="error",
                    payload={
                        "query": exception_context.statement.strip() if exception_context.statement else "",
                        "params": sanitize_sql_params(exception_context.parameters),
                        "database": getattr(engine.dialect, "name", "sql"),
                        "error": str(exception_context.original_exception),
                        "error_type": type(exception_context.original_exception).__name__
                    }
                )
        except ImportError:
            pass
