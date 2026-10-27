"""
Genesis Blackbox: Unified 1-Line Auto-Instrumentation
Provides one-call runtime instrumentation for Python web frameworks (FastAPI, Starlette, Flask, Django),
SQL databases (SQLAlchemy, sqlite3, DB-API), subprocess execution, and unhandled exception capture.
"""

import sys
import inspect
from typing import Any, Optional, Tuple, Union

from .recorder.wal_ring import WalRingBuffer
from .recorder.exception_hook import ExceptionHook, install_exception_hook
from .recorder.http_interceptor import BlackboxASGIMiddleware, BlackboxWSGIMiddleware
from .recorder.sql_interceptor import SqlInterceptor, TracedConnection
from .recorder.process_hook import SubprocessInterceptor


def auto_instrument(
    app: Any = None,
    engine: Any = None,
    wal_path: Union[str, WalRingBuffer] = ".blackbox/wal.sqlite"
) -> Any:
    """
    Unified 1-line auto-instrumentor for any Python application or microservice.

    Parameters:
        app: Web application instance (FastAPI, Starlette, Flask, Django, or raw ASGI/WSGI callable).
        engine: Database connection or SQLAlchemy engine instance.
        wal_path: Filepath for the circular SQLite WAL ring-buffer or an existing WalRingBuffer instance.

    Returns:
        The instrumented app, connection, or a tuple (app, engine) depending on inputs provided.
    """
    # 1. Initialize or resolve WAL ring-buffer
    if isinstance(wal_path, WalRingBuffer):
        wal_ring = wal_path
    else:
        wal_ring = WalRingBuffer(db_path=str(wal_path))

    # 2. Automatically install exception hook
    install_exception_hook(wal_ring=wal_ring)

    # 3. Automatically install subprocess interceptor
    SubprocessInterceptor.install_global(wal_ring=wal_ring)

    # 4. Instrument database connection or SQLAlchemy engine
    instrumented_engine = None
    if engine is not None:
        if hasattr(engine, "dialect") or hasattr(engine, "_has_events"):
            # SQLAlchemy Engine
            interceptor = SqlInterceptor(wal_ring)
            interceptor.hook_sqlalchemy(engine)
            instrumented_engine = engine
        elif hasattr(engine, "cursor"):
            # Standard DB-API connection (e.g., sqlite3.Connection)
            instrumented_engine = TracedConnection(engine, wal_ring=wal_ring)
        else:
            instrumented_engine = engine

    # 5. Instrument web application (ASGI vs WSGI)
    instrumented_app = None
    if app is not None:
        # Case A: FastAPI / Starlette application with add_middleware
        if hasattr(app, "add_middleware") and callable(getattr(app, "add_middleware")):
            app.add_middleware(BlackboxASGIMiddleware, wal_ring=wal_ring)
            instrumented_app = app
        # Case B: Flask application with wsgi_app
        elif hasattr(app, "wsgi_app"):
            app.wsgi_app = BlackboxWSGIMiddleware(app.wsgi_app, wal_ring=wal_ring)
            instrumented_app = app
        # Case C: Async callable (ASGI)
        elif (
            inspect.iscoroutinefunction(app)
            or (hasattr(app, "__call__") and inspect.iscoroutinefunction(app.__call__))
        ):
            instrumented_app = BlackboxASGIMiddleware(app, wal_ring=wal_ring)
        # Case D: Standard WSGI callable (Django, raw WSGI)
        elif callable(app):
            instrumented_app = BlackboxWSGIMiddleware(app, wal_ring=wal_ring)
        else:
            instrumented_app = app

    # Return appropriate instrumented references
    if app is not None and engine is not None:
        return instrumented_app, instrumented_engine
    if app is not None:
        return instrumented_app
    if engine is not None:
        return instrumented_engine
    return wal_ring
