"""
Unit and integration tests for Genesis Blackbox Phase 6:
- SubprocessInterceptor with subprocess.Popen and subprocess.run
- Argument and credential sanitization
- 1-line auto-instrumentation for ASGI, WSGI, DB engines, and exception handling
- CausalCrashCompiler integration with SUBPROCESS events in .blackbox.md
"""

import os
import sys
import time
import sqlite3
import subprocess
import pytest
from typing import Dict, Any, List

from blackbox.recorder.wal_ring import WalRingBuffer
from blackbox.recorder.process_hook import SubprocessInterceptor, sanitize_command_and_args
from blackbox.recorder.exception_hook import ExceptionHook, install_exception_hook, uninstall_exception_hook
from blackbox.recorder.http_interceptor import BlackboxASGIMiddleware, BlackboxWSGIMiddleware
from blackbox.recorder.sql_interceptor import TracedConnection
from blackbox.instrumentation import auto_instrument
from blackbox.compiler.autopsy import CausalCrashCompiler
from blackbox.ast_engine.graph import CodebaseGraph


@pytest.fixture(autouse=True)
def cleanup_hooks():
    """Ensure interceptors and hooks are clean between tests."""
    yield
    SubprocessInterceptor.uninstall()
    uninstall_exception_hook()


def test_subprocess_argument_sanitization():
    """Validates that sensitive flags and tokens are properly redacted."""
    # List argument form
    args = ["curl", "-H", "Authorization: Bearer secret-token-12345", "--api-key=abcdef", "--password", "supersecret", "https://user:pass@example.com/api"]
    cmd_name, clean_args = sanitize_command_and_args(args)
    assert cmd_name == "curl"
    assert clean_args[0] == "curl"
    assert clean_args[4] == "--password"
    assert clean_args[5] == "[REDACTED]"
    assert "--api-key=[REDACTED]" in clean_args[3]
    assert "https://user:[REDACTED]@example.com/api" in clean_args[6]

    # String argument form
    cmd_str = "python worker.py --token mysecret --db-pass=pwd123"
    c_name, clean_str = sanitize_command_and_args(cmd_str)
    assert c_name == "python"
    assert "--token [REDACTED]" in clean_str
    assert "--db-pass=[REDACTED]" in clean_str


def test_subprocess_run_capture_and_latency(tmp_path):
    """Validates capture in WAL of subprocess.run with duration_ms and exit code."""
    db_file = str(tmp_path / "wal.sqlite")
    ring = WalRingBuffer(db_path=db_file)

    interceptor = SubprocessInterceptor(wal_ring=ring)
    interceptor.install()

    try:
        # 1. Success execution
        res = subprocess.run(
            [sys.executable, "-c", "import time; time.sleep(0.01); print('hello stdout')"],
            capture_output=True,
            text=True
        )
        assert res.returncode == 0
        assert "hello stdout" in res.stdout

        # 2. Failed execution with stderr
        res_err = subprocess.run(
            [sys.executable, "-c", "import sys; sys.stderr.write('fatal crash occurred'); sys.exit(42)"],
            capture_output=True,
            text=True
        )
        assert res_err.returncode == 42
    finally:
        interceptor.uninstall()

    events = ring.query_recent(limit=10, event_type="SUBPROCESS")
    assert len(events) >= 2

    # Verify error event
    err_event = events[0]
    assert err_event["status"] == "error"
    assert err_event["duration_ms"] is not None and err_event["duration_ms"] > 0
    p_err = err_event["payload"]
    assert p_err["returncode"] == 42
    assert "fatal crash occurred" in p_err["stderr"]

    # Verify success event
    succ_event = events[1]
    assert succ_event["status"] == "ok"
    assert succ_event["duration_ms"] is not None
    p_succ = succ_event["payload"]
    assert p_succ["returncode"] == 0
    assert p_succ["stderr"] is None


def test_subprocess_popen_capture(tmp_path):
    """Validates capture in WAL of subprocess.Popen directly."""
    db_file = str(tmp_path / "wal.sqlite")
    ring = WalRingBuffer(db_path=db_file)

    with SubprocessInterceptor(wal_ring=ring):
        proc = subprocess.Popen(
            [sys.executable, "-c", "print('popen test')"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        stdout, stderr = proc.communicate()
        assert proc.returncode == 0
        assert "popen test" in stdout

    events = ring.query_recent(limit=5, event_type="SUBPROCESS")
    assert len(events) >= 1
    ev = events[0]
    assert ev["status"] == "ok"
    assert ev["payload"]["returncode"] == 0


def test_auto_instrument_asgi_app(tmp_path):
    """Validates 1-line auto_instrument with FastAPI/Starlette-like ASGI app."""
    db_file = str(tmp_path / "wal.sqlite")

    class MockFastAPIApp:
        def __init__(self):
            self.middlewares = []

        def add_middleware(self, middleware_cls, **kwargs):
            self.middlewares.append((middleware_cls, kwargs))

        async def __call__(self, scope, receive, send):
            pass

    mock_app = MockFastAPIApp()
    res = auto_instrument(app=mock_app, wal_path=db_file)

    assert res is mock_app
    assert len(mock_app.middlewares) == 1
    cls, kwargs = mock_app.middlewares[0]
    assert cls is BlackboxASGIMiddleware
    assert isinstance(kwargs.get("wal_ring"), WalRingBuffer)


def test_auto_instrument_wsgi_app(tmp_path):
    """Validates 1-line auto_instrument with Flask-like WSGI app."""
    db_file = str(tmp_path / "wal.sqlite")

    class MockFlaskApp:
        def __init__(self):
            self.wsgi_app = lambda environ, start_response: [b"Hello WSGI"]

    mock_flask = MockFlaskApp()
    res = auto_instrument(app=mock_flask, wal_path=db_file)

    assert res is mock_flask
    assert isinstance(mock_flask.wsgi_app, BlackboxWSGIMiddleware)


def test_auto_instrument_db_connection(tmp_path):
    """Validates 1-line auto_instrument with DB connection."""
    db_file = str(tmp_path / "wal.sqlite")
    raw_conn = sqlite3.connect(":memory:")

    instrumented_conn = auto_instrument(engine=raw_conn, wal_path=db_file)
    assert isinstance(instrumented_conn, TracedConnection)

    # Verify query works and records into WAL
    cur = instrumented_conn.cursor()
    cur.execute("CREATE TABLE users (id INT, name TEXT);")
    cur.execute("INSERT INTO users VALUES (1, 'Alice');")

    wal_ring = WalRingBuffer(db_path=db_file)
    sql_events = wal_ring.query_recent(limit=5, event_type="sql_query")
    assert len(sql_events) >= 2


def test_autopsy_capsule_includes_subprocess_events(tmp_path):
    """Validates presence of SUBPROCESS executions in compiled .blackbox.md autopsy."""
    db_file = str(tmp_path / "wal.sqlite")
    ring = WalRingBuffer(db_path=db_file)

    # 1. Execute subprocess under interceptor
    with SubprocessInterceptor(wal_ring=ring):
        subprocess.run(
            [sys.executable, "-c", "import sys; sys.exit(0)"],
            capture_output=True
        )
        subprocess.run(
            [sys.executable, "-c", "import sys; sys.stderr.write('npm build failed'); sys.exit(1)"],
            capture_output=True
        )

    # 2. Trigger an exception recorded in WAL
    hook = ExceptionHook(wal_ring=ring)
    try:
        raise RuntimeError("Microservice orchestration failed due to subprocess exit")
    except RuntimeError:
        exc_type, exc_val, tb = sys.exc_info()
        hook.handle_exception(exc_type, exc_val, tb)

    # 3. Compile autopsy capsule
    graph = CodebaseGraph()
    compiler = CausalCrashCompiler(wal_ring=ring, ast_graph=graph)
    capsule = compiler.compile()

    # 4. Verify presence in .blackbox.md
    assert "# 🚨 BLACKBOX CRASH AUTOPSY CAPSULE" in capsule
    assert "Microservice orchestration failed" in capsule
    assert "### Recent Subprocess Executions" in capsule
    assert "npm build failed" in capsule or "SUBPROCESS" in capsule
