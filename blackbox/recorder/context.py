"""
Genesis Blackbox: Async-Safe Trace Context Propagation
Provides contextvars-backed TraceContext for isolating concurrent async requests
(asyncio.gather) and synchronous WSGI/script spans without cross-contamination.
"""

import contextvars
import uuid
from typing import Any, Dict, Optional

_current_trace_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "blackbox_trace_id", default=None
)
_current_span_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "blackbox_span_id", default=None
)


def get_current_trace_id() -> Optional[str]:
    """Returns the active trace_id in the current async/sync execution context."""
    return _current_trace_id.get()


def get_current_span_id() -> Optional[str]:
    """Returns the active span_id in the current async/sync execution context."""
    return _current_span_id.get()


def extract_trace_id_from_headers(headers: Optional[Dict[str, Any]]) -> str:
    """
    Extracts trace_id from X-Trace-ID, X-Request-ID, or W3C traceparent headers,
    falling back to a short 16-char UUID hex identifier.
    """
    if isinstance(headers, dict):
        lowered = {str(k).lower(): str(v) for k, v in headers.items() if v is not None}
        for key in ("x-trace-id", "x-request-id"):
            val = lowered.get(key, "").strip()
            if val:
                return val
        traceparent = lowered.get("traceparent", "").strip()
        if traceparent:
            parts = traceparent.split("-")
            if len(parts) >= 2 and parts[1]:
                return parts[1]
            return traceparent
    return uuid.uuid4().hex[:16]


class _SpanContextManager:
    """Dual synchronous/asynchronous context manager binding trace_id and span_id."""

    def __init__(self, trace_id: Optional[str] = None, span_id: Optional[str] = None):
        self._requested_trace_id = trace_id
        self._requested_span_id = span_id
        self.trace_id: Optional[str] = None
        self.span_id: Optional[str] = None
        self._trace_token: Optional[contextvars.Token] = None
        self._span_token: Optional[contextvars.Token] = None

    def _activate(self) -> "_SpanContextManager":
        tid = (self._requested_trace_id or "").strip() or _current_trace_id.get() or uuid.uuid4().hex[:16]
        sid = (self._requested_span_id or "").strip() or uuid.uuid4().hex[:12]
        self.trace_id = tid
        self.span_id = sid
        self._trace_token = _current_trace_id.set(tid)
        self._span_token = _current_span_id.set(sid)
        return self

    def _deactivate(self) -> None:
        if self._span_token is not None:
            _current_span_id.reset(self._span_token)
            self._span_token = None
        if self._trace_token is not None:
            _current_trace_id.reset(self._trace_token)
            self._trace_token = None

    def __enter__(self) -> "_SpanContextManager":
        return self._activate()

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self._deactivate()

    async def __aenter__(self) -> "_SpanContextManager":
        return self._activate()

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        self._deactivate()


class TraceContext:
    """Factory and context manager for async-safe Blackbox trace/span propagation."""

    def __init__(self, trace_id: Optional[str] = None, span_id: Optional[str] = None):
        self._cm = _SpanContextManager(trace_id=trace_id, span_id=span_id)

    @staticmethod
    def span(trace_id: Optional[str] = None, span_id: Optional[str] = None) -> _SpanContextManager:
        """Creates a sync/async context manager for a trace span."""
        return _SpanContextManager(trace_id=trace_id, span_id=span_id)

    def __enter__(self) -> _SpanContextManager:
        return self._cm.__enter__()

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self._cm.__exit__(exc_type, exc_val, exc_tb)

    async def __aenter__(self) -> _SpanContextManager:
        return await self._cm.__aenter__()

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self._cm.__aexit__(exc_type, exc_val, exc_tb)

