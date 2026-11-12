"""
Unit tests for Genesis Blackbox v0.6.0:
- EgressMocker (outbound urllib capture, URL/secret sanitization, offline mock synthesis)
- CrashReproducer integration with offline HTTP_CLIENT egress stubs
- ResourceProfiler (stdlib tracemalloc + /proc/self/fd + autopsy footprint section)
- AutonomousHealPlanner, CLI `heal-plan --json`, and MCP `get_autonomous_heal_plan` (14 tools)
"""

import ast
import http.server
import json
import threading
import urllib.request
from pathlib import Path

from blackbox.cli import main as cli_main
from blackbox.compiler.autopsy import CausalCrashCompiler
from blackbox.compiler.heal_planner import AutonomousHealPlanner
from blackbox.compiler.reproducer import CrashReproducer
from blackbox.mcp_server import BlackboxMCPServer
from blackbox.recorder.context import TraceContext
from blackbox.recorder.egress_mocker import EgressMocker
from blackbox.recorder.resource_profiler import ResourceProfiler
from blackbox.recorder.wal_ring import WalRingBuffer


def _start_local_echo_server():
    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return None

        def do_GET(self):
            payload = {
                "order_id": 42,
                "api_key": "super-secret-value",
                "status": "ok",
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Authorization", "Bearer top-secret")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv, thread


def test_egress_mocker_capture_sanitizes_and_synthesizes_valid_mock_block(tmp_path: Path):
    srv, _ = _start_local_echo_server()
    port = srv.server_address[1]
    wal_db = str(tmp_path / "egress.sqlite")

    try:
        with WalRingBuffer(db_path=wal_db) as ring:
            with TraceContext.span(trace_id="egress-tr-1"):
                with EgressMocker(wal_ring=ring):
                    url = f"http://127.0.0.1:{port}/v1/checkout?user=alice&token=secret-tok-999"
                    with urllib.request.urlopen(url, timeout=5) as resp:
                        raw = json.loads(resp.read().decode("utf-8"))
                        assert raw["order_id"] == 42

            events = ring.get_timeline(limit=50)
            stubs = EgressMocker.extract_egress_stubs(events, trace_id="egress-tr-1")
            assert len(stubs) == 1
            stub = stubs[0]
            assert stub["method"] == "GET"
            assert stub["status_code"] == 200
            assert "token=[REDACTED]" in stub["url"]
            assert "secret-tok-999" not in stub["url"]
            assert "user=alice" in stub["url"]

            parsed_body = json.loads(stub["response_body"])
            assert parsed_body["order_id"] == 42
            assert parsed_body["api_key"] == "[REDACTED]"

            mock_block = EgressMocker.synthesize_python_mock_block(stubs)
            assert "unittest.mock" in mock_block
            assert "urllib.request.urlopen" in mock_block
            ast.parse(mock_block)
    finally:
        srv.shutdown()
        srv.server_close()


def test_crash_reproducer_embeds_offline_egress_mocks_from_http_client_events(tmp_path: Path):
    target_src = tmp_path / "billing_client.py"
    target_src.write_text(
        "import json\n"
        "import urllib.request\n\n"
        "def fetch_invoice(invoice_id):\n"
        "    with urllib.request.urlopen(f'http://offline.invalid/invoices/{invoice_id}?token=xyz') as resp:\n"
        "        data = json.loads(resp.read().decode('utf-8'))\n"
        "    if data.get('amount') is None:\n"
        "        raise ValueError('missing invoice amount')\n"
        "    return data['amount']\n",
        encoding="utf-8",
    )

    wal_db = str(tmp_path / "repro_egress.sqlite")
    with WalRingBuffer(db_path=wal_db) as ring:
        ring.record(
            "HTTP_CLIENT",
            {
                "method": "GET",
                "url": "http://offline.invalid/invoices/77?token=secret",
                "status_code": 200,
                "response_body": json.dumps({"id": 77, "amount": None}),
                "response_headers": {"content-type": "application/json"},
            },
            source="egress_mocker",
            trace_id="trace-off-1",
        )
        ring.record(
            "EXCEPTION",
            {
                "exception_type": "ValueError",
                "exception_message": "missing invoice amount",
                "root_cause": {
                    "filename": str(target_src),
                    "function": "fetch_invoice",
                    "lineno": 8,
                    "locals": {"invoice_id": 77},
                },
                "frames": [
                    {
                        "filename": str(target_src),
                        "function": "fetch_invoice",
                        "lineno": 8,
                        "locals": {"invoice_id": 77},
                    }
                ],
            },
            status="error",
            trace_id="trace-off-1",
        )

        code = CrashReproducer(wal_ring=ring).generate()
        ast.parse(code)
        assert "_blackbox_mock_egress" in code
        assert "urllib.request.urlopen" in code
        assert "offline.invalid/invoices/77" in code


def test_resource_profiler_captures_tracemalloc_and_renders_autopsy_section(tmp_path: Path):
    wal_db = str(tmp_path / "resource.sqlite")
    profiler = ResourceProfiler(peak_threshold_kb=1.0, delta_threshold_kb=0.1)

    with WalRingBuffer(db_path=wal_db) as ring:
        with TraceContext.span(trace_id="res-tr-1"):
            with profiler.profile_scope(wal_ring=ring, label="checkout_batch") as metrics:
                buffer = [bytearray(4096) for _ in range(16)]
                assert len(buffer) == 16

        assert metrics["label"] == "checkout_batch"
        assert metrics["peak_kb"] > 0
        assert metrics["open_fds"] is None or isinstance(metrics["open_fds"], int)
        assert len(metrics["top_allocations"]) > 0

        ring.record(
            "EXCEPTION",
            {
                "exception_type": "MemoryError",
                "exception_message": "allocation limit hit",
                "root_cause": {
                    "filename": __file__,
                    "function": "test_resource_profiler_captures_tracemalloc_and_renders_autopsy_section",
                    "lineno": 1,
                    "locals": {"batch_size": 16},
                },
                "frames": [],
            },
            status="error",
            trace_id="res-tr-1",
        )

        autopsy_md = CausalCrashCompiler(
            wal_ring=ring,
            resource_peak_threshold_kb=1.0,
        ).compile()
        assert "## Resource & Memory Footprint" in autopsy_md
        assert "checkout_batch" in autopsy_md
        assert "ALERT (threshold exceeded)" in autopsy_md
        assert "### Top Memory Allocation Sites" in autopsy_md


def _seed_full_incident_workspace(tmp_path: Path):
    src_file = tmp_path / "orders.py"
    src_file.write_text(
        "def process_order(order_id, multiplier):\n"
        "    if multiplier is None:\n"
        "        raise ValueError('multiplier cannot be None')\n"
        "    return order_id * multiplier\n",
        encoding="utf-8",
    )
    test_file = tmp_path / "test_orders.py"
    test_file.write_text(
        "from orders import process_order\n"
        "def test_process_order():\n"
        "    assert process_order(3, 2) == 6\n",
        encoding="utf-8",
    )

    baseline_db = tmp_path / "wal.baseline.sqlite"
    with WalRingBuffer(db_path=str(baseline_db)) as b_ring:
        b_ring.record("PROCESS_START", {"run_id": "base-ok"}, source="test")
        b_ring.record("SQL_QUERY", {"query": "SELECT * FROM orders WHERE id = 1"}, duration_ms=2.0)
        b_ring.record("PROCESS_EXIT", {"exit_code": 0}, source="test")

    wal_db = tmp_path / "wal.sqlite"
    with WalRingBuffer(db_path=str(wal_db)) as w_ring:
        w_ring.record("PROCESS_START", {"run_id": "fail-1"}, source="test", trace_id="heal-tr-1")
        w_ring.record(
            "HTTP_CLIENT",
            {
                "method": "GET",
                "url": "https://pricing.internal/rate?api_key=abc123",
                "status_code": 200,
                "response_body": {"multiplier": None},
            },
            source="egress_mocker",
            trace_id="heal-tr-1",
        )
        for i in range(5):
            w_ring.record(
                "SQL_QUERY",
                {"query": f"SELECT * FROM orders WHERE id = {i}"},
                duration_ms=80.0,
                trace_id="heal-tr-1",
            )
        w_ring.record(
            "RESOURCE_PROFILE",
            {"label": "process_order", "current_kb": 42.0, "peak_kb": 64.0, "delta_kb": 12.0, "open_fds": 8},
            source="resource_profiler",
            trace_id="heal-tr-1",
        )
        w_ring.record(
            "EXCEPTION",
            {
                "exception_type": "ValueError",
                "exception_message": "multiplier cannot be None",
                "root_cause": {
                    "filename": str(src_file),
                    "function": "process_order",
                    "lineno": 3,
                    "locals": {"order_id": 10, "multiplier": None},
                },
                "frames": [
                    {
                        "filename": str(src_file),
                        "function": "process_order",
                        "lineno": 3,
                        "locals": {"order_id": 10, "multiplier": None},
                    }
                ],
            },
            source="exception_hook",
            status="error",
            trace_id="heal-tr-1",
        )
        w_ring.record("PROCESS_EXIT", {"exit_code": 1}, source="test", status="error", trace_id="heal-tr-1")

    return wal_db, baseline_db, src_file, test_file


def test_autonomous_heal_planner_combines_crash_stubs_diff_and_tests(tmp_path: Path):
    wal_db, baseline_db, src_file, test_file = _seed_full_incident_workspace(tmp_path)
    planner = AutonomousHealPlanner()
    plan = planner.build_heal_plan(
        wal_path=str(wal_db),
        baseline_path=str(baseline_db),
        root_dir=str(tmp_path),
    )

    assert plan["has_crash"] is True
    assert plan["root_cause"]["exception_type"] == "ValueError"
    assert len(plan["egress_stubs"]) == 1
    assert "api_key=[REDACTED]" in plan["egress_stubs"][0]["url"]
    assert plan["resource_profile"]["peak_kb"] == 64.0
    assert len(plan["performance_alerts"]["n_plus_one_queries"]) == 1
    assert plan["baseline_divergence"] is not None
    assert str(src_file) in [str(Path(p)) for p in plan["affected_files"]]
    assert any(Path(t).name == test_file.name for t in plan["impacted_tests"])
    assert len(plan["steps"]) == 4
    assert [s["phase"] for s in plan["steps"]] == [
        "reproduce_offline",
        "inspect_frames_and_diff",
        "validate_candidate_patch",
        "verify_and_emit_receipt",
    ]
    assert "# Genesis Blackbox Autonomous Heal Plan" in plan["markdown_plan"]


def test_cli_heal_plan_json(tmp_path: Path, capsys):
    wal_db, baseline_db, _, _ = _seed_full_incident_workspace(tmp_path)
    rc = cli_main([
        "heal-plan",
        "--db", str(wal_db),
        "--baseline", str(baseline_db),
        "--root", str(tmp_path),
        "--json",
    ])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["has_crash"] is True
    assert len(data["steps"]) == 4
    assert len(data["egress_stubs"]) == 1


def test_mcp_get_autonomous_heal_plan_and_14_tools_list(tmp_path: Path):
    wal_db, baseline_db, _, _ = _seed_full_incident_workspace(tmp_path)
    server = BlackboxMCPServer(db_path=str(wal_db))
    try:
        tools_resp = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tool_names = [t["name"] for t in tools_resp["result"]["tools"]]
        assert len(tool_names) == 14
        assert "get_autonomous_heal_plan" in tool_names

        heal_resp = server.handle_request({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "get_autonomous_heal_plan",
                "arguments": {
                    "db_path": str(wal_db),
                    "baseline_db_path": str(baseline_db),
                    "root_dir": str(tmp_path),
                },
            },
        })
        heal_payload = json.loads(heal_resp["result"]["content"][0]["text"])
        assert heal_payload["has_crash"] is True
        assert len(heal_payload["steps"]) == 4
        assert len(heal_payload["egress_stubs"]) == 1

        ctx_resp = server.handle_request({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "get_full_incident_context",
                "arguments": {
                    "db_path": str(wal_db),
                    "baseline_db_path": str(baseline_db),
                    "root_dir": str(tmp_path),
                },
            },
        })
        ctx_payload = json.loads(ctx_resp["result"]["content"][0]["text"])
        assert len(ctx_payload["egress_stubs"]) == 1
        assert ctx_payload["resource_profile"]["peak_kb"] == 64.0
    finally:
        server.close()
