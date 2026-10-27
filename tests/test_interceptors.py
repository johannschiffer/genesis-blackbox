"""
Tests for Genesis Blackbox HTTP and SQL Runtime Interceptors
Validates:
1. Low-latency HTTP lifecycle capture (<1ms).
2. Automated sensitive credential redaction (Authorization, Cookie, Password).
3. Interception of successful and failed SQL queries with exact error traces.
"""

import asyncio
import sqlite3
import json
import pytest

from blackbox.recorder.wal_ring import WalRingBuffer
from blackbox.recorder.http_interceptor import (
    BlackboxASGIMiddleware,
    BlackboxWSGIMiddleware,
    HttpTraceContext,
    sanitize_headers,
    sanitize_payload
)
from blackbox.recorder.sql_interceptor import (
    SqlInterceptor,
    TracedCursor,
    SqlTraceContext,
    sanitize_sql_params
)


@pytest.fixture
def wal_ring():
    ring = WalRingBuffer(db_path=":memory:", max_records=1000)
    yield ring
    ring.close()


# -----------------------------------------------------------------------------
# HTTP INTERCEPTOR TESTS
# -----------------------------------------------------------------------------

def test_sanitize_headers_and_body_redaction():
    headers = [
        (b"authorization", b"Bearer super-secret-jwt-token"),
        (b"cookie", b"session_id=abcdef123456"),
        (b"content-type", b"application/json"),
        (b"x-custom-trace", b"trace-123")
    ]
    cleaned_headers = sanitize_headers(headers)
    assert cleaned_headers["authorization"] == "[REDACTED]"
    assert cleaned_headers["cookie"] == "[REDACTED]"
    assert cleaned_headers["content-type"] == "application/json"
    assert cleaned_headers["x-custom-trace"] == "trace-123"

    body_json = json.dumps({
        "username": "alex",
        "password": "supersecretpassword123",
        "nested": {
            "api_key": "secret-key-999",
            "active": True
        }
    })
    cleaned_body = sanitize_payload(body_json)
    parsed = json.loads(cleaned_body)
    assert parsed["username"] == "alex"
    assert parsed["password"] == "[REDACTED]"
    assert parsed["nested"]["api_key"] == "[REDACTED]"
    assert parsed["nested"]["active"] is True


def test_asgi_middleware_capture_and_latency(wal_ring):
    async def dummy_app(scope, receive, send):
        body_msg = await receive()
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")]
        })
        await send({
            "type": "http.response.body",
            "body": b'{"status": "success"}'
        })

    middleware = BlackboxASGIMiddleware(dummy_app, wal_ring)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/auth/login",
        "query_string": b"redirect=/dashboard",
        "headers": [
            (b"authorization", b"Bearer token-xyz"),
            (b"content-type", b"application/json")
        ]
    }

    async def mock_receive():
        return {
            "type": "http.request",
            "body": b'{"user": "alice", "password": "mypassword"}'
        }

    sent_messages = []
    async def mock_send(msg):
        sent_messages.append(msg)

    asyncio.run(middleware(scope, mock_receive, mock_send))

    events = wal_ring.query_recent(limit=10, event_type="http_transaction")
    assert len(events) == 1
    ev = events[0]

    assert ev["status"] == "ok"
    assert ev["source"] == "asgi"
    assert ev["duration_ms"] >= 0.0
    # Sub-millisecond or low latency overhead check
    assert ev["duration_ms"] < 10.0

    payload = ev["payload"]
    assert payload["method"] == "POST"
    assert payload["path"] == "/api/v1/auth/login"
    assert payload["request_headers"]["authorization"] == "[REDACTED]"
    req_body = json.loads(payload["request_body"])
    assert req_body["password"] == "[REDACTED]"
    assert req_body["user"] == "alice"
    assert payload["status_code"] == 200


def test_wsgi_middleware_capture(wal_ring):
    def dummy_wsgi(environ, start_response):
        start_response("201 Created", [("Content-Type", "text/plain")])
        return [b"Resource created"]

    middleware = BlackboxWSGIMiddleware(dummy_wsgi, wal_ring)

    environ = {
        "REQUEST_METHOD": "PUT",
        "PATH_INFO": "/items/42",
        "QUERY_STRING": "cache=false",
        "HTTP_AUTHORIZATION": "Bearer secret-bearer",
        "HTTP_COOKIE": "user=admin; token=abc",
        "CONTENT_TYPE": "application/json"
    }

    def mock_start_response(status, headers, exc_info=None):
        return None

    result = list(middleware(environ, mock_start_response))
    assert b"".join(result) == b"Resource created"

    events = wal_ring.query_recent(limit=5, event_type="http_transaction")
    assert len(events) == 1
    ev = events[0]
    assert ev["source"] == "wsgi"
    assert ev["payload"]["status_code"] == 201
    assert ev["payload"]["request_headers"]["authorization"] == "[REDACTED]"
    assert ev["payload"]["request_headers"]["cookie"] == "[REDACTED]"


def test_http_trace_context(wal_ring):
    with HttpTraceContext(wal_ring, "GET", "https://api.github.com/zen") as ctx:
        ctx.set_request(headers={"Authorization": "token 12345"})
        ctx.set_response(status_code=503, body="Service Unavailable")

    events = wal_ring.query_recent(limit=5, event_type="http_transaction")
    assert len(events) == 1
    ev = events[0]
    assert ev["status"] == "error"
    assert ev["payload"]["status_code"] == 503
    assert ev["payload"]["request_headers"]["authorization"] == "[REDACTED]"


# -----------------------------------------------------------------------------
# SQL INTERCEPTOR TESTS
# -----------------------------------------------------------------------------

def test_sql_interceptor_sqlite3_success_and_redaction(wal_ring):
    interceptor = SqlInterceptor(wal_ring)
    raw_conn = sqlite3.connect(":memory:")
    conn = interceptor.hook_sqlite3_connection(raw_conn)

    cursor = conn.cursor()
    cursor.execute("CREATE TABLE users (id INT, username TEXT, password TEXT, secret_token TEXT);")
    cursor.execute(
        "INSERT INTO users VALUES (:id, :username, :password, :secret_token);",
        {
            "id": 1,
            "username": "charlie",
            "password": "cleartextpassword",
            "secret_token": "token_12345"
        }
    )

    events = wal_ring.query_recent(limit=10, event_type="sql_query")
    # Must have 2 recorded SQL queries
    assert len(events) == 2

    insert_ev = events[0]  # most recent (DESC order)
    assert insert_ev["status"] == "ok"
    assert insert_ev["source"] == "sqlite3"
    assert insert_ev["duration_ms"] >= 0.0

    payload = insert_ev["payload"]
    assert "INSERT INTO users" in payload["query"]
    assert payload["params"]["username"] == "charlie"
    assert payload["params"]["password"] == "[REDACTED]"
    assert payload["params"]["secret_token"] == "[REDACTED]"
    assert payload["rows_affected"] == 1


def test_sql_interceptor_failed_query_error_recording(wal_ring):
    interceptor = SqlInterceptor(wal_ring)
    raw_conn = sqlite3.connect(":memory:")
    conn = interceptor.hook_sqlite3_connection(raw_conn)

    cursor = conn.cursor()
    with pytest.raises(sqlite3.OperationalError):
        cursor.execute("SELECT * FROM non_existent_table_xyz WHERE id = 1;")

    events = wal_ring.query_recent(limit=5, event_type="sql_query")
    assert len(events) == 1
    ev = events[0]

    assert ev["status"] == "error"
    assert ev["payload"]["error_type"] == "OperationalError"
    assert "no such table: non_existent_table_xyz" in ev["payload"]["error"]
    assert "SELECT * FROM non_existent_table_xyz" in ev["payload"]["query"]


def test_sql_trace_context_direct(wal_ring):
    interceptor = SqlInterceptor(wal_ring)

    with interceptor.trace("UPDATE inventory SET stock = ? WHERE item_id = ?", [50, "SKU-99"]) as tracer:
        tracer.set_rows_affected(5)

    events = wal_ring.query_recent(limit=5, event_type="sql_query")
    assert len(events) == 1
    ev = events[0]
    assert ev["status"] == "ok"
    assert ev["payload"]["rows_affected"] == 5
    assert ev["payload"]["params"] == [50, "SKU-99"]
