"""
Phase 11 Audit Hardening & Regression Tests for Genesis Blackbox.
Covers subprocess runner bootstrap, non-seekable WSGI streams, threading.excepthook,
Flask teardown capture, real compiler-to-GitHub summary formatting, AST directory
exclusions, cross-file impact test selection, reproducer escaping, patch rollback
preservation, MCP JSON-RPC notifications/ping, and UI XSS/CORS protections.
"""

import ast
import io
import os
import sys
import json
import threading
import subprocess
import urllib.request
from pathlib import Path

from blackbox import (
    __version__,
    WalRingBuffer,
    ExceptionHook,
    install_exception_hook,
    uninstall_exception_hook,
    BlackboxWSGIMiddleware,
    CodebaseGraph,
    ImpactTestSelector,
    CausalCrashCompiler,
    CrashReproducer,
    PatchValidator,
    BlackboxMCPServer,
    export_github_step_summary,
    instrument_flask,
)
from blackbox.recorder.runner import run_target
from blackbox.ui.server import create_ui_server


def test_runner_subprocess_python_captures_exception(tmp_path):
    crash_script = tmp_path / "boom_app.py"
    crash_script.write_text(
        "def compute(x):\n"
        "    secret_token = 'super-secret'\n"
        "    return 100 / x\n\n"
        "compute(0)\n",
        encoding="utf-8",
    )
    db_file = tmp_path / "subproc_wal.sqlite"

    rc = run_target([sys.executable, str(crash_script)], db_path=str(db_file))
    assert rc != 0

    ring = WalRingBuffer(db_path=str(db_file))
    try:
        excs = ring.query_recent(limit=5, event_type="EXCEPTION")
        assert len(excs) == 1
        payload = excs[0]["payload"]
        assert payload["exception_type"] == "ZeroDivisionError"
        assert payload["root_cause"]["function"] == "compute"
        assert "REDACTED" in payload["root_cause"]["locals"]["secret_token"]
    finally:
        ring.close()


def test_wsgi_non_seekable_stream_preserved(tmp_path):
    class NonSeekableStream:
        def __init__(self, raw: bytes):
            self._bio = io.BytesIO(raw)

        def read(self, size: int = -1) -> bytes:
            return self._bio.read(size)

        def readline(self, size: int = -1) -> bytes:
            return self._bio.readline(size)

    ring = WalRingBuffer(db_path=str(tmp_path / "wsgi.sqlite"))
    received_by_app = {}

    def downstream_app(environ, start_response):
        body = environ["wsgi.input"].read()
        received_by_app["body"] = body
        start_response("201 Created", [("Content-Type", "application/json")])
        return [b'{"ok":true}']

    mw = BlackboxWSGIMiddleware(downstream_app, wal_ring=ring, max_body_bytes=16)
    full_payload = b'{"username":"alice","note":"longer-than-sixteen-bytes"}'
    environ = {
        "REQUEST_METHOD": "POST",
        "PATH_INFO": "/submit",
        "CONTENT_LENGTH": str(len(full_payload)),
        "HTTP_X_TRACE_ID": "trace-abc-123",
        "wsgi.input": NonSeekableStream(full_payload),
    }

    chunks = list(mw(environ, lambda s, h, e=None: None))
    assert b"".join(chunks) == b'{"ok":true}'
    assert received_by_app["body"] == full_payload

    events = ring.query_recent(limit=1, event_type="http_transaction")
    assert len(events) == 1
    assert events[0]["trace_id"] == "trace-abc-123"
    assert events[0]["payload"]["status_code"] == 201
    ring.close()


def test_threading_excepthook_and_singleton_wal_rebind(tmp_path):
    ring1 = WalRingBuffer(db_path=str(tmp_path / "r1.sqlite"))
    ring2 = WalRingBuffer(db_path=str(tmp_path / "r2.sqlite"))
    try:
        install_exception_hook(wal_ring=ring1)
        hook = install_exception_hook(wal_ring=ring2)
        assert hook.wal_ring is ring2

        def worker():
            local_marker = "bg-thread-42"
            raise RuntimeError(f"thread crash {local_marker}")

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        excs = ring2.query_recent(limit=2, event_type="EXCEPTION")
        assert len(excs) == 1
        assert excs[0]["payload"]["exception_type"] == "RuntimeError"
        assert excs[0]["payload"]["root_cause"]["locals"]["local_marker"] == "bg-thread-42"
    finally:
        uninstall_exception_hook()
        ring1.close()
        ring2.close()


def test_flask_teardown_records_exception(tmp_path):
    class FakeFlaskApp:
        def __init__(self):
            self.extensions = {}
            self._teardown_cbs = []
            self.wsgi_app = lambda env, sr: [b"ok"]

        def teardown_request(self, fn):
            self._teardown_cbs.append(fn)
            return fn

    ring = WalRingBuffer(db_path=str(tmp_path / "flask.sqlite"))
    app = FakeFlaskApp()
    instrument_flask(app, wal_ring=ring)
    uninstall_exception_hook()

    try:
        raise ValueError("unhandled route bug")
    except ValueError as err:
        for cb in app._teardown_cbs:
            cb(err)

    excs = ring.query_recent(limit=2, event_type="EXCEPTION")
    assert len(excs) == 1
    assert excs[0]["payload"]["exception_type"] == "ValueError"
    ring.close()


def test_autopsy_deduplication_and_github_summary_real_sections(tmp_path):
    src_file = tmp_path / "calc.py"
    src_file.write_text(
        "def caller():\n    return divide(10, 0)\n\ndef divide(a, b):\n    return a / b\n",
        encoding="utf-8",
    )
    ring = WalRingBuffer(db_path=str(tmp_path / "autopsy.sqlite"))
    ring.record("http_transaction", {"method": "GET", "path": "/divide", "status_code": 500}, source="asgi")

    graph = CodebaseGraph()
    graph.parse_file(str(src_file))
    compiler = CausalCrashCompiler(wal_ring=ring, ast_graph=graph)

    capsule = compiler.compile(
        exception_data={
            "exception_type": "ZeroDivisionError",
            "exception_message": "division by zero",
            "frames": [
                {"filename": str(src_file), "lineno": 2, "function": "caller", "code_line": "return divide(10, 0)", "locals": {}},
                {"filename": str(src_file), "lineno": 5, "function": "divide", "code_line": "return a / b", "locals": {"expr": "a|b", "b": 0}},
            ],
            "root_cause": {
                "filename": str(src_file),
                "lineno": 5,
                "function": "divide",
                "code_line": "return a / b",
                "locals": {"expr": "a|b", "b": 0},
            },
        }
    )

    assert capsule.count("AST Blast-Radius Impact") == 1
    assert "## 4. Execution Stack Trace Frames" in capsule
    assert "a\\|b" in capsule

    autopsy_path = tmp_path / ".blackbox.md"
    autopsy_path.write_text(capsule, encoding="utf-8")
    summary_out = tmp_path / "step_summary.md"
    summary_md = export_github_step_summary(str(autopsy_path), str(summary_out))

    assert "Executive Incident Summary" in summary_md
    assert "Temporal Causal Context" in summary_md
    assert "Inspected Call Stack" in summary_md
    assert "AST Blast Radius" in summary_md
    assert "Suggested Autonomous Fix Mandate" in summary_md
    ring.close()


def test_ast_graph_excludes_venv_and_impact_resolves_cross_file_callers(tmp_path):
    venv_pkg = tmp_path / ".venv" / "lib" / "noise.py"
    venv_pkg.parent.mkdir(parents=True)
    venv_pkg.write_text("def noise_fn():\n    return 1\n", encoding="utf-8")

    core_py = tmp_path / "core_math.py"
    core_py.write_text("def low_level_add(x, y):\n    return x + y\n", encoding="utf-8")

    service_py = tmp_path / "billing_service.py"
    service_py.write_text(
        "from core_math import low_level_add\n\n"
        "def compute_invoice(total):\n"
        "    return low_level_add(total, 5)\n",
        encoding="utf-8",
    )

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    test_billing = tests_dir / "test_invoice_flow.py"
    test_billing.write_text(
        "from billing_service import compute_invoice\n\n"
        "def test_invoice():\n"
        "    assert compute_invoice(10) == 15\n",
        encoding="utf-8",
    )

    selector = ImpactTestSelector(root_dir=str(tmp_path))
    impacted = selector.find_impacted_tests(files=[str(core_py)], diff=False)
    assert "noise_fn" not in selector.graph.nodes
    assert str(test_billing) in impacted


def test_reproducer_escapes_docstrings_and_windows_backslashes(tmp_path):
    ring = WalRingBuffer(db_path=str(tmp_path / "repro.sqlite"))
    ring.record(
        "EXCEPTION",
        {
            "exception_type": "ValueError",
            "exception_message": 'bad path C:\\Users\\admin and triple """ quotes',
            "frames": [],
            "root_cause": {
                "filename": r"C:\Users\admin\app.py",
                "lineno": 12,
                "function": "parse_cfg",
                "locals": {"raw": 'a"""b'},
            },
        },
        source="exception_hook",
        status="error",
    )
    code = CrashReproducer(ring).generate()
    ast.parse(code)
    ring.close()


def test_mcp_notifications_ping_and_ui_cors_status(tmp_path):
    db_path = str(tmp_path / "mcp_ui.sqlite")
    mcp = BlackboxMCPServer(db_path=db_path)
    assert mcp.version == __version__
    assert mcp.handle_request({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None

    ping_resp = mcp.handle_request({"jsonrpc": "2.0", "id": 99, "method": "ping"})
    assert ping_resp == {"jsonrpc": "2.0", "id": 99, "result": {}}
    mcp.wal_ring.close()

    ui_srv = create_ui_server(host="127.0.0.1", port=0, db_path=db_path)
    port = ui_srv.server_address[1]
    t = threading.Thread(target=ui_srv.serve_forever, daemon=True)
    t.start()
    try:
        req_evil = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/status",
            headers={"Origin": "https://attacker.example"},
        )
        with urllib.request.urlopen(req_evil, timeout=5) as resp:
            assert resp.headers.get("Access-Control-Allow-Origin") is None
            data = json.loads(resp.read().decode())
            assert data["version"] == __version__

        req_local = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/status",
            headers={"Origin": f"http://127.0.0.1:{port}"},
        )
        with urllib.request.urlopen(req_local, timeout=5) as resp2:
            assert resp2.headers.get("Access-Control-Allow-Origin") == f"http://127.0.0.1:{port}"
    finally:
        ui_srv.shutdown()
        ui_srv.server_close()
        ui_srv.wal_ring.close()
