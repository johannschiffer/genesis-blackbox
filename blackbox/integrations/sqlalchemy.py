"""
Genesis Blackbox: Idiomatic SQLAlchemy Integration
Registers before_cursor_execute, after_cursor_execute, and handle_error event listeners
with automated bind parameter redaction and latency recording.
"""

from typing import Any
from ..recorder.wal_ring import WalRingBuffer
from ..recorder.sql_interceptor import SqlInterceptor


def instrument_engine(engine: Any, wal_path: str = ".blackbox/wal.sqlite", **kwargs) -> Any:
    """
    Instruments a SQLAlchemy Engine instance with Blackbox query tracing.
    Automatically logs executed statements, bind parameters (with credential redaction),
    and failure states into WAL.
    """
    wal_ring = kwargs.pop("wal_ring", None)
    if wal_ring is None:
        wal_ring = WalRingBuffer(db_path=str(wal_path))

    interceptor = SqlInterceptor(wal_ring)
    interceptor.hook_sqlalchemy(engine)
    return engine
