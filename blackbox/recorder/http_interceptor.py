"""
Genesis Blackbox: Universal HTTP Interceptor
Provides non-blocking ASGI and WSGI middleware, plus client hooks,
to record inbound/outbound HTTP transactions directly into the WAL ring-buffer.
"""

import time
import json
import uuid
from typing import Dict, Any, List, Optional, Callable, Tuple
from urllib.parse import parse_qs

from .wal_ring import WalRingBuffer

SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "proxy-authorization",
    "token",
    "x-auth-token"
}

SENSITIVE_BODY_KEYS = {
    "password", "secret", "token", "auth", "access_token",
    "api_key", "credential", "private_key"
}


def sanitize_headers(headers: Any) -> Dict[str, str]:
    """Sanitizes HTTP headers, redacting bearer tokens and cookies."""
    sanitized = {}
    if isinstance(headers, (list, tuple)):
        for item in headers:
            if len(item) == 2:
                k, v = item
                if isinstance(k, bytes):
                    k = k.decode("latin1", errors="replace")
                if isinstance(v, bytes):
                    v = v.decode("latin1", errors="replace")
                k_norm = str(k).lower().strip()
                if k_norm in SENSITIVE_HEADERS:
                    sanitized[k_norm] = "[REDACTED]"
                else:
                    sanitized[k_norm] = str(v)
    elif isinstance(headers, dict):
        for k, v in headers.items():
            k_norm = str(k).lower().strip()
            if k_norm in SENSITIVE_HEADERS:
                sanitized[k_norm] = "[REDACTED]"
            else:
                sanitized[k_norm] = str(v)
    return sanitized


def sanitize_payload(payload: Any, max_len: int = 2000) -> Any:
    """Sanitizes and bounds request/response body payloads."""
    if payload is None:
        return None

    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except Exception:
            return f"<binary: {len(payload)} bytes>"

    if isinstance(payload, str):
        # Attempt JSON parse to redact sensitive keys
        stripped = payload.strip()
        if (stripped.startswith("{") and stripped.endswith("}")) or (stripped.startswith("[") and stripped.endswith("]")):
            try:
                parsed = json.loads(stripped)
                redacted = _redact_dict(parsed)
                out_str = json.dumps(redacted)
                return out_str[:max_len] + ("..." if len(out_str) > max_len else "")
            except Exception:
                pass
        return payload[:max_len] + ("..." if len(payload) > max_len else "")

    if isinstance(payload, dict):
        return _redact_dict(payload)

    return str(payload)[:max_len]


def _redact_dict(data: Any) -> Any:
    if isinstance(data, dict):
        cleaned = {}
        for k, v in data.items():
            k_lower = str(k).lower()
            if any(s in k_lower for s in SENSITIVE_BODY_KEYS):
                cleaned[k] = "[REDACTED]"
            else:
                cleaned[k] = _redact_dict(v)
        return cleaned
    elif isinstance(data, list):
        return [_redact_dict(item) for item in data]
    return data


class BlackboxASGIMiddleware:
    """
    Standard ASGI 3 middleware that records incoming HTTP requests and
    outbound responses into the Blackbox circular WAL buffer.
    """

    def __init__(
        self,
        app: Any,
        wal_ring: WalRingBuffer,
        max_body_bytes: int = 4096
    ):
        self.app = app
        self.wal_ring = wal_ring
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Dict[str, Any], receive: Callable, send: Callable) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        path = scope.get("path", "/")
        query_string = scope.get("query_string", b"").decode("latin1")
        headers = sanitize_headers(scope.get("headers", []))
        trace_id = headers.get("x-trace-id") or headers.get("x-request-id")

        start_time = time.perf_counter()
        request_body_chunks = []
        status_code = [200]
        response_headers = [{}]
        response_body_chunks = []

        async def intercepted_receive():
            message = await receive()
            if message.get("type") == "http.request":
                body = message.get("body", b"")
                if body and sum(len(c) for c in request_body_chunks) < self.max_body_bytes:
                    request_body_chunks.append(body)
            return message

        async def intercepted_send(message):
            if message.get("type") == "http.response.start":
                status_code[0] = message.get("status", 200)
                response_headers[0] = sanitize_headers(message.get("headers", []))
            elif message.get("type") == "http.response.body":
                body = message.get("body", b"")
                if body and sum(len(c) for c in response_body_chunks) < self.max_body_bytes:
                    response_body_chunks.append(body)
            await send(message)

        status = "ok"
        error_msg = None
        try:
            await self.app(scope, intercepted_receive, intercepted_send)
        except Exception as exc:
            status = "error"
            error_msg = str(exc)
            status_code[0] = 500
            raise
        finally:
            duration_ms = (time.perf_counter() - start_time) * 1000.0
            req_body_raw = b"".join(request_body_chunks)
            res_body_raw = b"".join(response_body_chunks)

            payload = {
                "method": method,
                "path": path,
                "query": query_string,
                "request_headers": headers,
                "request_body": sanitize_payload(req_body_raw),
                "status_code": status_code[0],
                "response_headers": response_headers[0],
                "response_body": sanitize_payload(res_body_raw),
            }
            if error_msg:
                payload["error"] = error_msg

            if status_code[0] >= 500:
                status = "error"

            self.wal_ring.record(
                event_type="http_transaction",
                source="asgi",
                duration_ms=round(duration_ms, 3),
                trace_id=trace_id,
                status=status,
                payload=payload
            )


class _ReplayingWSGIInput:
    """Preserves prefetched WSGI request bytes ahead of a non-seekable input stream."""

    def __init__(self, prefix: bytes, stream: Any):
        self._buf = bytearray(prefix)
        self._stream = stream

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            head = bytes(self._buf)
            self._buf.clear()
            tail = self._stream.read() if self._stream else b""
            return head + tail
        if size <= len(self._buf):
            out = bytes(self._buf[:size])
            del self._buf[:size]
            return out
        head = bytes(self._buf)
        self._buf.clear()
        rem = size - len(head)
        tail = self._stream.read(rem) if self._stream else b""
        return head + tail

    def readline(self, size: int = -1) -> bytes:
        nl = self._buf.find(b"\n")
        if nl != -1:
            end = nl + 1 if (size is None or size < 0) else min(nl + 1, size)
            out = bytes(self._buf[:end])
            del self._buf[:end]
            return out
        if size is not None and size >= 0 and len(self._buf) >= size:
            out = bytes(self._buf[:size])
            del self._buf[:size]
            return out
        head = bytes(self._buf)
        self._buf.clear()
        if not self._stream:
            return head
        rem_size = -1 if (size is None or size < 0) else (size - len(head))
        tail = self._stream.readline(rem_size) if rem_size >= 0 else self._stream.readline()
        return head + tail

    def readlines(self, hint: int = -1) -> List[bytes]:
        lines: List[bytes] = []
        total = 0
        while True:
            line = self.readline()
            if not line:
                break
            lines.append(line)
            total += len(line)
            if hint is not None and hint > 0 and total >= hint:
                break
        return lines

    def __iter__(self):
        while True:
            line = self.readline()
            if not line:
                break
            yield line


class BlackboxWSGIMiddleware:
    """
    Standard WSGI middleware that captures synchronous HTTP request/response
    lifecycles into the Blackbox circular WAL buffer.
    """

    def __init__(
        self,
        app: Any,
        wal_ring: WalRingBuffer,
        max_body_bytes: int = 4096
    ):
        self.app = app
        self.wal_ring = wal_ring
        self.max_body_bytes = max_body_bytes

    def __call__(self, environ: Dict[str, Any], start_response: Callable) -> Any:
        start_time = time.perf_counter()
        method = environ.get("REQUEST_METHOD", "GET")
        path = environ.get("PATH_INFO", "/")
        query_string = environ.get("QUERY_STRING", "")

        raw_headers = []
        for k, v in environ.items():
            if k.startswith("HTTP_"):
                header_name = k[5:].replace("_", "-").lower()
                raw_headers.append((header_name, v))
            elif k in ("CONTENT_TYPE", "CONTENT_LENGTH"):
                raw_headers.append((k.replace("_", "-").lower(), v))

        headers = sanitize_headers(raw_headers)
        trace_id = headers.get("x-trace-id") or headers.get("x-request-id")

        req_body = None
        content_length = environ.get("CONTENT_LENGTH")
        if content_length and str(content_length).isdigit() and int(content_length) > 0:
            length = min(int(content_length), self.max_body_bytes)
            input_stream = environ.get("wsgi.input")
            if input_stream:
                try:
                    req_body = input_stream.read(length)
                    environ["wsgi.input"] = _ReplayingWSGIInput(req_body, input_stream)
                except Exception:
                    pass

        status_code = [200]
        resp_headers = [{}]

        def intercepted_start_response(status, response_headers, exc_info=None):
            try:
                status_code[0] = int(status.split()[0])
            except Exception:
                status_code[0] = 200
            resp_headers[0] = sanitize_headers(response_headers)
            return start_response(status, response_headers, exc_info)

        status = "ok"
        error_msg = None
        resp_chunks = []
        captured_resp_bytes = 0
        result = None
        try:
            result = self.app(environ, intercepted_start_response)
            for chunk in result:
                if isinstance(chunk, bytes) and captured_resp_bytes < self.max_body_bytes:
                    take = chunk[: self.max_body_bytes - captured_resp_bytes]
                    resp_chunks.append(take)
                    captured_resp_bytes += len(take)
                yield chunk
        except Exception as exc:
            status = "error"
            error_msg = str(exc)
            status_code[0] = 500
            raise
        finally:
            if result is not None and hasattr(result, "close"):
                try:
                    result.close()
                except Exception:
                    pass
            duration_ms = (time.perf_counter() - start_time) * 1000.0
            res_body_raw = b"".join(resp_chunks)

            payload = {
                "method": method,
                "path": path,
                "query": query_string,
                "request_headers": headers,
                "request_body": sanitize_payload(req_body),
                "status_code": status_code[0],
                "response_headers": resp_headers[0],
                "response_body": sanitize_payload(res_body_raw),
            }
            if error_msg:
                payload["error"] = error_msg

            if status_code[0] >= 500:
                status = "error"

            self.wal_ring.record(
                event_type="http_transaction",
                source="wsgi",
                duration_ms=round(duration_ms, 3),
                trace_id=trace_id,
                status=status,
                payload=payload
            )


class HttpTraceContext:
    """Context manager for tracing arbitrary client or server HTTP operations."""

    def __init__(self, wal_ring: WalRingBuffer, method: str, url: str, source: str = "http_client"):
        self.wal_ring = wal_ring
        self.method = method.upper()
        self.url = url
        self.source = source
        self.start_time = 0.0
        self.status_code = 200
        self.request_headers: Dict[str, str] = {}
        self.request_body: Any = None
        self.response_headers: Dict[str, str] = {}
        self.response_body: Any = None
        self.status = "ok"
        self.error: Optional[str] = None

    def __enter__(self):
        self.start_time = time.perf_counter()
        return self

    def set_request(self, headers: Any = None, body: Any = None):
        if headers:
            self.request_headers = sanitize_headers(headers)
        if body:
            self.request_body = sanitize_payload(body)

    def set_response(self, status_code: int, headers: Any = None, body: Any = None):
        self.status_code = status_code
        if headers:
            self.response_headers = sanitize_headers(headers)
        if body:
            self.response_body = sanitize_payload(body)
        if status_code >= 400:
            self.status = "error"

    def __exit__(self, exc_type, exc_val, exc_tb):
        duration_ms = (time.perf_counter() - self.start_time) * 1000.0
        if exc_val:
            self.status = "error"
            self.error = str(exc_val)
            if self.status_code < 400:
                self.status_code = 500

        payload = {
            "method": self.method,
            "url": self.url,
            "request_headers": self.request_headers,
            "request_body": self.request_body,
            "status_code": self.status_code,
            "response_headers": self.response_headers,
            "response_body": self.response_body
        }
        if self.error:
            payload["error"] = self.error

        self.wal_ring.record(
            event_type="http_transaction",
            source=self.source,
            duration_ms=round(duration_ms, 3),
            status=self.status,
            payload=payload
        )
