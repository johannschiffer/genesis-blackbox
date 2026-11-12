"""
Genesis Blackbox: Offline Egress HTTP Interceptor & Mock Synthesizer
Captures outbound `urllib.request.urlopen` calls into the WAL with URL query and secret
redaction, extracts trace-scoped egress stubs, and synthesizes standalone `unittest.mock`
blocks for 100% offline deterministic crash reproduction.
"""

import io
import json
import time
import urllib.error
import urllib.request
from email.message import Message
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, quote_plus, urlsplit, urlunsplit

from .http_interceptor import _redact_dict, sanitize_headers, sanitize_payload
from .wal_ring import WalRingBuffer

_SENSITIVE_QUERY_KEYS = (
    "token",
    "secret",
    "password",
    "api_key",
    "apikey",
    "credential",
    "private_key",
    "auth",
    "bearer",
    "session",
    "sig",
    "signature",
)


def _is_sensitive_query_key(key: str) -> bool:
    k_lower = str(key or "").strip().lower()
    if k_lower in ("key", "sig", "signature"):
        return True
    return any(tok in k_lower for tok in _SENSITIVE_QUERY_KEYS)


class _BufferedHTTPResponse:
    """In-memory wrapper preserving `http.client.HTTPResponse` behavior after body capture."""

    def __init__(
        self,
        body: bytes,
        status: Optional[int],
        headers: Any,
        url: str,
    ):
        self._stream = io.BytesIO(body)
        self.status = status
        self.code = status
        self.headers = headers
        self._url = url
        self.closed = False

    def read(self, amt: Optional[int] = -1) -> bytes:
        if amt is None:
            return self._stream.read()
        return self._stream.read(amt)

    def readline(self, limit: int = -1) -> bytes:
        return self._stream.readline(limit)

    def readlines(self, hint: int = -1) -> List[bytes]:
        return self._stream.readlines(hint)

    def getcode(self) -> Optional[int]:
        return self.status

    def geturl(self) -> str:
        return self._url

    def info(self) -> Any:
        return self.headers

    def close(self) -> None:
        self.closed = True
        self._stream.close()

    def __iter__(self):
        return iter(self._stream)

    def __enter__(self) -> "_BufferedHTTPResponse":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


class EgressMocker:
    """
    Context manager hooking `urllib.request.urlopen` to capture outbound HTTP traffic,
    sanitize secrets from URLs/headers/bodies, and synthesize offline Python mock blocks.
    """

    def __init__(
        self,
        wal_ring: Optional[WalRingBuffer] = None,
        max_body_len: int = 2000,
    ):
        self.wal_ring = wal_ring
        self.max_body_len = max_body_len
        self._orig_urlopen = None
        self._installed = False

    @staticmethod
    def sanitize_url(url: str) -> str:
        """Sanitizes URL userinfo credentials and sensitive query parameter values."""
        raw = str(url or "")
        if not raw:
            return ""
        try:
            parts = urlsplit(raw)
        except Exception:
            return raw

        netloc = parts.netloc
        if "@" in netloc:
            netloc = netloc.split("@", 1)[1]

        sanitized_pairs: List[str] = []
        if parts.query:
            for k, v in parse_qsl(parts.query, keep_blank_values=True):
                encoded_k = quote_plus(str(k))
                if _is_sensitive_query_key(k):
                    sanitized_pairs.append(f"{encoded_k}=[REDACTED]")
                else:
                    sanitized_pairs.append(f"{encoded_k}={quote_plus(str(v))}")
        sanitized_query = "&".join(sanitized_pairs)
        return urlunsplit((parts.scheme, netloc, parts.path, sanitized_query, parts.fragment))

    def install(self) -> "EgressMocker":
        """Installs the `urllib.request.urlopen` interception wrapper."""
        if self._installed:
            return self

        orig_urlopen = urllib.request.urlopen
        self._orig_urlopen = orig_urlopen
        mocker = self

        def _wrapped_urlopen(url_or_req, data=None, timeout=None, *args, **kwargs):
            if isinstance(url_or_req, urllib.request.Request):
                raw_url = url_or_req.full_url
                method = (url_or_req.get_method() or ("POST" if data is not None else "GET")).upper()
                req_headers = dict(url_or_req.header_items())
            else:
                raw_url = str(url_or_req)
                method = "POST" if data is not None else "GET"
                req_headers = {}

            call_kwargs = dict(kwargs)
            if timeout is not None:
                call_kwargs["timeout"] = timeout

            t0 = time.perf_counter()
            try:
                resp = orig_urlopen(url_or_req, data, *args, **call_kwargs)
            except urllib.error.HTTPError as exc:
                dur_ms = (time.perf_counter() - t0) * 1000.0
                err_body = b""
                try:
                    err_body = exc.read()
                except Exception:
                    pass
                mocker._record_egress(
                    method=method,
                    raw_url=raw_url,
                    status_code=getattr(exc, "code", 500) or 500,
                    req_headers=req_headers,
                    resp_headers=dict(exc.headers.items()) if getattr(exc, "headers", None) else {},
                    body_bytes=err_body,
                    duration_ms=dur_ms,
                    status="error",
                )
                raise
            except Exception as exc:
                dur_ms = (time.perf_counter() - t0) * 1000.0
                mocker._record_egress(
                    method=method,
                    raw_url=raw_url,
                    status_code=0,
                    req_headers=req_headers,
                    resp_headers={},
                    body_bytes=str(exc).encode("utf-8", errors="replace"),
                    duration_ms=dur_ms,
                    status="error",
                )
                raise

            dur_ms = (time.perf_counter() - t0) * 1000.0
            status_code = getattr(resp, "status", None)
            if status_code is None and hasattr(resp, "getcode"):
                status_code = resp.getcode()
            resp_headers_obj = getattr(resp, "headers", None)
            resp_headers_dict = dict(resp_headers_obj.items()) if hasattr(resp_headers_obj, "items") else {}
            resp_url = resp.geturl() if hasattr(resp, "geturl") else raw_url

            raw_bytes = b""
            try:
                raw_bytes = resp.read()
            finally:
                try:
                    resp.close()
                except Exception:
                    pass

            mocker._record_egress(
                method=method,
                raw_url=raw_url,
                status_code=int(status_code or 200),
                req_headers=req_headers,
                resp_headers=resp_headers_dict,
                body_bytes=raw_bytes,
                duration_ms=dur_ms,
                status="ok" if int(status_code or 200) < 400 else "error",
            )
            return _BufferedHTTPResponse(
                body=raw_bytes,
                status=status_code,
                headers=resp_headers_obj if resp_headers_obj is not None else Message(),
                url=resp_url,
            )

        urllib.request.urlopen = _wrapped_urlopen
        self._installed = True
        return self

    def uninstall(self) -> None:
        """Restores the original `urllib.request.urlopen` function."""
        if self._installed and self._orig_urlopen is not None:
            urllib.request.urlopen = self._orig_urlopen
        self._installed = False
        self._orig_urlopen = None

    def __enter__(self) -> "EgressMocker":
        return self.install()

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.uninstall()

    def _record_egress(
        self,
        method: str,
        raw_url: str,
        status_code: int,
        req_headers: Dict[str, Any],
        resp_headers: Dict[str, Any],
        body_bytes: bytes,
        duration_ms: float,
        status: str,
    ) -> None:
        if not self.wal_ring:
            return
        self.wal_ring.record(
            event_type="HTTP_CLIENT",
            payload={
                "method": method,
                "url": self.sanitize_url(raw_url),
                "status_code": status_code,
                "request_headers": sanitize_headers(req_headers),
                "response_headers": sanitize_headers(resp_headers),
                "response_body": sanitize_payload(body_bytes, max_len=self.max_body_len),
            },
            source="egress_mocker",
            duration_ms=round(duration_ms, 3),
            status=status,
        )

    @classmethod
    def extract_egress_stubs(
        cls,
        events: List[Dict[str, Any]],
        trace_id: Optional[str] = None,
        max_body_len: int = 2000,
    ) -> List[Dict[str, Any]]:
        """
        Extracts deterministic, sanitized outbound HTTP stubs from WAL events,
        optionally filtered to a specific `trace_id`.
        """
        if not events:
            return []

        candidates: List[Dict[str, Any]] = []
        for idx, ev in enumerate(events):
            if not isinstance(ev, dict):
                continue
            etype = ev.get("event_type", "")
            payload = ev.get("payload")
            if not isinstance(payload, dict):
                continue

            if etype == "HTTP_CLIENT":
                pass
            elif etype == "http_transaction":
                if payload.get("direction") == "inbound" or not payload.get("url"):
                    continue
            else:
                continue

            ev_trace = ev.get("trace_id") or payload.get("trace_id")
            if trace_id is not None and ev_trace != trace_id:
                continue

            candidates.append((ev.get("timestamp") or 0.0, ev.get("id") or idx, idx, ev, payload, ev_trace))

        candidates.sort(key=lambda item: (float(item[0]), int(item[1]), int(item[2])))

        stubs: List[Dict[str, Any]] = []
        for _, _, _, ev, payload, ev_trace in candidates:
            raw_url = str(payload.get("url") or payload.get("path") or "/")
            method = str(payload.get("method", "GET")).upper()
            status_code = int(payload.get("status_code") or payload.get("status") or 200)
            resp_headers = sanitize_headers(payload.get("response_headers") or {})

            raw_body = payload.get("response_body")
            if raw_body is None:
                raw_body = payload.get("body")
            if raw_body is None:
                sanitized_body = ""
            elif isinstance(raw_body, (dict, list)):
                redacted = _redact_dict(raw_body)
                encoded = json.dumps(redacted)
                sanitized_body = encoded[:max_body_len] + ("..." if len(encoded) > max_body_len else "")
            else:
                sanitized_body = str(sanitize_payload(raw_body, max_len=max_body_len))

            stubs.append({
                "method": method,
                "url": cls.sanitize_url(raw_url),
                "status_code": status_code,
                "response_headers": resp_headers,
                "response_body": sanitized_body,
                "trace_id": ev_trace,
                "duration_ms": ev.get("duration_ms"),
            })

        return stubs

    @classmethod
    def synthesize_python_mock_block(cls, stubs: List[Dict[str, Any]]) -> str:
        """
        Synthesizes a standalone, syntactically valid Python mock block patching
        `urllib.request.urlopen` using `unittest.mock.patch` for offline test replay.
        """
        serialized_stubs = [
            {
                "method": str(s.get("method", "GET")).upper(),
                "url": cls.sanitize_url(str(s.get("url", "/"))),
                "status_code": int(s.get("status_code", 200)),
                "response_headers": dict(s.get("response_headers") or {}),
                "response_body": str(s.get("response_body") or ""),
            }
            for s in stubs
        ]

        lines = [
            "import io as _bb_io",
            "from unittest.mock import patch as _bb_patch",
            "from urllib.parse import urlsplit as _bb_urlsplit",
            "",
            f"_BLACKBOX_EGRESS_STUBS = {repr(serialized_stubs)}",
            "_blackbox_egress_cursor = 0",
            "",
            "",
            "class _BlackboxMockHTTPResponse:",
            "    def __init__(self, body_bytes, status, headers, url):",
            "        self._stream = _bb_io.BytesIO(body_bytes)",
            "        self.status = status",
            "        self.code = status",
            "        self.headers = headers",
            "        self._url = url",
            "",
            "    def read(self, amt=-1):",
            "        return self._stream.read() if amt is None or amt < 0 else self._stream.read(amt)",
            "",
            "    def readline(self, limit=-1):",
            "        return self._stream.readline(limit)",
            "",
            "    def getcode(self):",
            "        return self.status",
            "",
            "    def geturl(self):",
            "        return self._url",
            "",
            "    def info(self):",
            "        return self.headers",
            "",
            "    def close(self):",
            "        self._stream.close()",
            "",
            "    def __enter__(self):",
            "        return self",
            "",
            "    def __exit__(self, exc_type, exc_val, exc_tb):",
            "        self.close()",
            "",
            "",
            "def _blackbox_mock_urlopen(req, *args, **kwargs):",
            "    global _blackbox_egress_cursor",
            "    req_url = getattr(req, 'full_url', str(req))",
            "    req_method = (req.get_method() if hasattr(req, 'get_method') else 'GET').upper()",
            "    req_parts = _bb_urlsplit(req_url)",
            "    chosen = None",
            "    for stub in _BLACKBOX_EGRESS_STUBS:",
            "        stub_parts = _bb_urlsplit(stub['url'])",
            "        if stub['method'] == req_method and stub_parts.netloc == req_parts.netloc and stub_parts.path == req_parts.path:",
            "            chosen = stub",
            "            break",
            "    if chosen is None and _BLACKBOX_EGRESS_STUBS:",
            "        chosen = _BLACKBOX_EGRESS_STUBS[min(_blackbox_egress_cursor, len(_BLACKBOX_EGRESS_STUBS) - 1)]",
            "        _blackbox_egress_cursor += 1",
            "    if chosen is None:",
            "        chosen = {'status_code': 200, 'response_headers': {}, 'response_body': '', 'url': req_url}",
            "    body_bytes = str(chosen.get('response_body', '')).encode('utf-8')",
            "    return _BlackboxMockHTTPResponse(",
            "        body_bytes=body_bytes,",
            "        status=int(chosen.get('status_code', 200)),",
            "        headers=dict(chosen.get('response_headers') or {}),",
            "        url=chosen.get('url', req_url),",
            "    )",
            "",
            "",
            "def _blackbox_mock_egress():",
            "    return _bb_patch('urllib.request.urlopen', side_effect=_blackbox_mock_urlopen)",
        ]
        return "\n".join(lines)
