"""
Genesis Blackbox: Idiomatic Flask & WSGI Integration
Attaches WSGI flight recorder middleware, request teardown listeners, and exception hooks.
"""

from typing import Any
from ..recorder.wal_ring import WalRingBuffer
from ..recorder.exception_hook import ExceptionHook, install_exception_hook
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
        if hasattr(app, "extensions") and isinstance(app.extensions, dict):
            app.extensions["blackbox_wal"] = wal_ring
        app.wsgi_app = BlackboxWSGIMiddleware(app.wsgi_app, wal_ring=wal_ring, **kwargs)

        if hasattr(app, "teardown_request") and callable(app.teardown_request):
            exc_recorder = ExceptionHook(wal_ring=wal_ring)

            @app.teardown_request
            def _blackbox_teardown(exception=None):
                if exception is not None and not getattr(exception, "_blackbox_recorded", False):
                    try:
                        setattr(exception, "_blackbox_recorded", True)
                    except Exception:
                        pass
                    exc_recorder.handle_exception(type(exception), exception, exception.__traceback__)

        return app

    return BlackboxWSGIMiddleware(app, wal_ring=wal_ring, **kwargs)
