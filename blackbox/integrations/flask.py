"""
Genesis Blackbox: Idiomatic Flask & WSGI Integration
Attaches WSGI flight recorder middleware, request teardown listeners, and exception hooks.
"""

from typing import Any
from ..recorder.wal_ring import WalRingBuffer
from ..recorder.exception_hook import install_exception_hook
from ..recorder.http_interceptor import BlackboxWSGIMiddleware
from ..recorder.process_hook import SubprocessInterceptor


def instrument_flask(app: Any, wal_path: str = ".blackbox/wal.sqlite", **kwargs) -> Any:
    """
    Instruments a Flask application with Blackbox flight recording.
    Wraps wsgi_app, registers teardown exception handlers, and activates subprocess tracking.
    """
    wal_ring = kwargs.pop("wal_ring", None)
    if wal_ring is None:
        wal_ring = WalRingBuffer(db_path=str(wal_path))

    install_exception_hook(wal_ring=wal_ring)
    SubprocessInterceptor.install_global(wal_ring=wal_ring)

    if hasattr(app, "wsgi_app"):
        app.wsgi_app = BlackboxWSGIMiddleware(app.wsgi_app, wal_ring=wal_ring, **kwargs)

        if hasattr(app, "teardown_request") and callable(app.teardown_request):
            @app.teardown_request
            def _blackbox_teardown(exception=None):
                pass

        return app

    return BlackboxWSGIMiddleware(app, wal_ring=wal_ring, **kwargs)
