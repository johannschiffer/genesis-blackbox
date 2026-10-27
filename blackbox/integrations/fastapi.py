"""
Genesis Blackbox: Idiomatic FastAPI & Starlette Integration
Attaches non-blocking ASGI flight recorder middleware and lifecycle exception hooks.
"""

from typing import Any
from ..recorder.wal_ring import WalRingBuffer
from ..recorder.exception_hook import install_exception_hook
from ..recorder.http_interceptor import BlackboxASGIMiddleware
from ..recorder.process_hook import SubprocessInterceptor


def instrument_fastapi(app: Any, wal_path: str = ".blackbox/wal.sqlite", **kwargs) -> Any:
    """
    Instruments a FastAPI or Starlette application with Blackbox flight recording.
    Injects BlackboxASGIMiddleware, unhandled exception capture, and subprocess tracking.
    """
    wal_ring = kwargs.pop("wal_ring", None)
    if wal_ring is None:
        wal_ring = WalRingBuffer(db_path=str(wal_path))

    install_exception_hook(wal_ring=wal_ring)
    SubprocessInterceptor.install_global(wal_ring=wal_ring)

    if hasattr(app, "add_middleware") and callable(app.add_middleware):
        app.add_middleware(BlackboxASGIMiddleware, wal_ring=wal_ring, **kwargs)
        return app

    return BlackboxASGIMiddleware(app, wal_ring=wal_ring, **kwargs)
