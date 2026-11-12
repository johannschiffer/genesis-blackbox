"""
Genesis Blackbox v0.5.0 Unit Tests
Validates:
1. Strict async TraceContext isolation across 5 concurrent ASGI requests via asyncio.gather.
2. N+1 SQL fingerprint normalization & detection + automatic inclusion in .blackbox.md.
3. Baseline latency regression detection across SQL, HTTP, and SUBPROCESS events.
4. FlakeAnalyzer classification of oscillating runs and non-determinism causes.
5. CLI subcommand `blackbox diagnose-perf --json`.
6. MCP tool `diagnose_performance_and_flakiness` and enrichment of `get_full_incident_context`.
"""

import asyncio
import json
from pathlib import Path

from blackbox.cli import main as cli_main
from blackbox.compiler.autopsy import CausalCrashCompiler
from blackbox.compiler.flake_analyzer import FlakeAnalyzer
from blackbox.compiler.perf_detector import PerformanceDetector
from blackbox.mcp_server import BlackboxMCPServer
from blackbox.recorder.context import TraceContext, get_current_span_id, get_current_trace_id
from blackbox.recorder.http_interceptor import BlackboxASGIMiddleware
from blackbox.recorder.wal_ring import WalRingBuffer


def test_async_trace_context_isolation_across_concurrent_asgi_requests(tmp_path: Path):
    wal_path = tmp_path / "async_wal.sqlite"
    ring = WalRingBuffer(db_path=str(wal_path))

    async def app(scope, receive, send):
        await receive()
        path = scope["path"]
        req_num = int(path.rsplit("/", 1)[-1])
        # Stagger async suspension points so requests interleave
        await asyncio.sleep(0.01 * (6 - req_num))
        ring.record(
            event_type="SQL_QUERY",
            payload={"query": f"SELECT * FROM orders WHERE req = {req_num}"},
            source="sqlite3",
            duration_ms=1.5,
        )
        await asyncio.sleep(0.005 * req_num)
        if req_num == 3:
            ring.record(
                event_type="EXCEPTION",
                payload={
                    "exception_type": "RuntimeError",
                    "exception_message": "boom in req-3",
                    "root_cause": {"filename": "app.py", "function": "handler", "lineno": 12, "locals": {}},
                },
                source="exception_hook",
                status="error",
            )
            await send({"type": "http.response.start", "status": 500, "headers": []})
            await send({"type": "http.response.body", "body": b"err"})
            return

        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = BlackboxASGIMiddleware(app, wal_ring=ring)

    async def fire_request(idx: int):
        scope = {
            "type": "http",
            "method": "GET",
            "path": f"/orders/{idx}",
            "query_string": b"",
            "headers": [(b"x-request-id", f"req-trace-{idx}".encode("ascii"))],
        }

        async def recv():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(msg):
            return None

        await mw(scope, recv, send)

    async def run_all():
        assert get_current_trace_id() is None
        assert get_current_span_id() is None
        await asyncio.gather(*(fire_request(i) for i in range(1, 6)))
        assert get_current_trace_id() is None
        assert get_current_span_id() is None

    try:
        asyncio.run(run_all())

        events = ring.get_timeline(limit=50)
        sql_events = [e for e in events if e["event_type"] == "SQL_QUERY"]
        http_events = [e for e in events if e["event_type"] == "http_transaction"]
        exc_events = [e for e in events if e["event_type"] == "EXCEPTION"]

        assert len(sql_events) == 5
        assert len(http_events) == 5
        assert len(exc_events) == 1

        # Verify each SQL query and crash stayed strictly bound to its own request trace_id
        for ev in sql_events:
            query_str = ev["payload"]["query"]
            req_idx = query_str.rsplit("=", 1)[-1].strip()
            expected_trace = f"req-trace-{req_idx}"
            assert ev["trace_id"] == expected_trace
            assert ev["payload"]["trace_id"] == expected_trace
            assert ev["span_id"] is not None
            assert ev["payload"]["span_id"] == ev["span_id"]

        assert exc_events[0]["trace_id"] == "req-trace-3"
        assert exc_events[0]["payload"]["trace_id"] == "req-trace-3"

        # Verify TraceContext direct sync usage also isolates and resets cleanly
        with TraceContext.span(trace_id="sync-outer") as outer:
            assert get_current_trace_id() == "sync-outer"
            assert get_current_span_id() == outer.span_id
        assert get_current_trace_id() is None
    finally:
        ring.close()


def test_n_plus_one_detection_and_autopsy_inclusion(tmp_path: Path):
    det = PerformanceDetector()
    fp1 = det.normalize_sql_fingerprint("SELECT  *  FROM   users WHERE id = 123 AND name = 'alice'")
    fp2 = det.normalize_sql_fingerprint('SELECT * FROM users WHERE id = 999 AND name = "bob"')
    assert fp1 == fp2 == "SELECT * FROM users WHERE id = ? AND name = ?"

    wal_path = tmp_path / "nplus1.sqlite"
    ring = WalRingBuffer(db_path=str(wal_path))
    try:
        for i in range(1, 9):
            ring.record(
                event_type="SQL_QUERY",
                payload={"query": f"SELECT * FROM line_items WHERE order_id = {i}"},
                source="sqlite3",
                duration_ms=2.5,
                trace_id="trace-nplus1",
            )
        ring.record(
            event_type="SQL_QUERY",
            payload={"query": "SELECT * FROM other_table WHERE id = 1"},
            source="sqlite3",
            duration_ms=1.0,
            trace_id="trace-other",
        )
        ring.record(
            event_type="EXCEPTION",
            payload={
                "exception_type": "TimeoutError",
                "exception_message": "DB pool exhausted",
                "root_cause": {"filename": "orders.py", "function": "load_orders", "lineno": 20, "locals": {}},
            },
            source="exception_hook",
            trace_id="trace-nplus1",
            status="error",
        )

        findings = det.detect_n_plus_one(ring.get_timeline(limit=50), threshold=5)
        assert len(findings) == 1
        assert findings[0]["fingerprint"] == "SELECT * FROM line_items WHERE order_id = ?"
        assert findings[0]["count"] == 8
        assert findings[0]["total_duration_ms"] == 20.0
        assert findings[0]["trace_id"] == "trace-nplus1"

        capsule = CausalCrashCompiler(wal_ring=ring).compile()
        assert "**Trace ID:** `trace-nplus1`" in capsule
        assert "## Performance & N+1 Query Diagnostics" in capsule
        assert "SELECT * FROM line_items WHERE order_id = ?" in capsule
        assert "8x" in capsule
        assert "[trace]" in capsule
    finally:
        ring.close()


def test_latency_regression_detection_against_baseline(tmp_path: Path):
    baseline_path = tmp_path / "wal.baseline.sqlite"
    wal_path = tmp_path / "wal.sqlite"

    b_ring = WalRingBuffer(db_path=str(baseline_path))
    b_ring.record("SQL_QUERY", {"query": "SELECT * FROM products WHERE id = 1"}, duration_ms=4.0)
    b_ring.record("SQL_QUERY", {"query": "SELECT * FROM healthcheck"}, duration_ms=5.0)
    b_ring.record("http_transaction", {"method": "GET", "path": "/api/items"}, duration_ms=15.0)
    b_ring.close()

    w_ring = WalRingBuffer(db_path=str(wal_path))
    w_ring.record("SQL_QUERY", {"query": "SELECT * FROM products WHERE id = 99"}, duration_ms=210.0, trace_id="tr-slow")
    w_ring.record("SQL_QUERY", {"query": "SELECT * FROM healthcheck"}, duration_ms=10.0)
    w_ring.record("http_transaction", {"method": "GET", "path": "/api/items"}, duration_ms=280.0, trace_id="tr-slow")
    w_ring.close()

    regressions = PerformanceDetector().detect_latency_regressions(
        wal_path=str(wal_path),
        baseline_path=str(baseline_path),
        min_delta_ms=50.0,
        factor=3.0,
    )
    assert len(regressions) == 2
    ops = {r["operation"]: r for r in regressions}
    assert "GET /api/items" in ops
    assert "SELECT * FROM products WHERE id = ?" in ops
    assert "SELECT * FROM healthcheck" not in ops
    assert ops["GET /api/items"]["delta_ms"] == 265.0
    assert ops["SELECT * FROM products WHERE id = ?"]["factor"] == 52.5


def test_flake_analyzer_classifies_oscillating_runs_and_suspect_causes(tmp_path: Path):
    wal_path = tmp_path / "flaky.sqlite"
    ring = WalRingBuffer(db_path=str(wal_path))
    try:
        # Run 1: passing
        ring.record("PROCESS_START", {"script": "test_checkout.py", "source_hash": "abc123"}, source="runner")
        ring.record("TEST_START", {"nodeid": "test_checkout.py::test_payment"}, source="pytest_plugin")
        ring.record("PROCESS_EXIT", {"script": "test_checkout.py", "exit_code": 0}, source="runner")

        # Run 2: failing with transient timeout + UUID reference (same source hash)
        ring.record("PROCESS_START", {"script": "test_checkout.py", "source_hash": "abc123"}, source="runner")
        ring.record("TEST_START", {"nodeid": "test_checkout.py::test_payment"}, source="pytest_plugin")
        ring.record(
            "EXCEPTION",
            {
                "exception_type": "TimeoutError",
                "exception_message": "timed out waiting for 550e8400-e29b-41d4-a716-446655440000",
                "root_cause": {"filename": "test_checkout.py", "function": "test_payment", "lineno": 10},
            },
            source="exception_hook",
            status="error",
        )
        ring.record("PROCESS_EXIT", {"script": "test_checkout.py", "exit_code": 1}, source="runner", status="error")

        # Run 3: passing again
        ring.record("PROCESS_START", {"script": "test_checkout.py", "source_hash": "abc123"}, source="runner")
        ring.record("TEST_START", {"nodeid": "test_checkout.py::test_payment"}, source="pytest_plugin")
        ring.record("PROCESS_EXIT", {"script": "test_checkout.py", "exit_code": 0}, source="runner")
    finally:
        ring.close()

    result = FlakeAnalyzer().analyze_flakiness(wal_path=str(wal_path), baseline_path=str(tmp_path / "missing.sqlite"))
    assert result["is_flaky"] is True
    assert result["confidence"] >= 0.65
    causes_blob = " ".join(result["suspect_causes"]).lower()
    assert "oscillated" in causes_blob
    assert "timeout" in causes_blob or "network" in causes_blob
    assert "uuid" in causes_blob or "timestamp" in causes_blob
    assert "FLAKY" in result["markdown_report"]


def test_cli_diagnose_perf_json(tmp_path: Path, capsys):
    wal_path = tmp_path / "wal.sqlite"
    ring = WalRingBuffer(db_path=str(wal_path))
    for i in range(6):
        ring.record("SQL_QUERY", {"query": f"SELECT id FROM jobs WHERE k = {i}"}, duration_ms=3.0, trace_id="cli-tr")
    ring.close()

    rc = cli_main([
        "diagnose-perf",
        "--db", str(wal_path),
        "--baseline", str(tmp_path / "missing.baseline.sqlite"),
        "--threshold", "5",
        "--json",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert set(data.keys()) == {
        "is_flaky",
        "confidence",
        "suspect_causes",
        "n_plus_one_queries",
        "latency_regressions",
        "markdown_report",
    }
    assert len(data["n_plus_one_queries"]) == 1
    assert data["n_plus_one_queries"][0]["count"] == 6


def test_mcp_diagnose_perf_and_full_incident_context_enrichment(tmp_path: Path):
    wal_path = tmp_path / "mcp_wal.sqlite"
    baseline_path = tmp_path / "mcp_wal.baseline.sqlite"

    b_ring = WalRingBuffer(db_path=str(baseline_path))
    b_ring.record("PROCESS_START", {"run_id": "b1"}, source="test")
    b_ring.record("SQL_QUERY", {"query": "SELECT * FROM items WHERE id = 1"}, duration_ms=2.0)
    b_ring.record("PROCESS_EXIT", {"exit_code": 0}, source="test")
    b_ring.close()

    w_ring = WalRingBuffer(db_path=str(wal_path))
    w_ring.record("PROCESS_START", {"run_id": "f1"}, source="test")
    for i in range(5):
        w_ring.record(
            "SQL_QUERY",
            {"query": f"SELECT * FROM items WHERE id = {i}"},
            duration_ms=120.0,
            trace_id="mcp-tr-1",
        )
    w_ring.record(
        "EXCEPTION",
        {
            "exception_type": "ValueError",
            "exception_message": "bad item",
            "root_cause": {"filename": "items.py", "function": "fetch", "lineno": 8, "locals": {}},
        },
        trace_id="mcp-tr-1",
        status="error",
    )
    w_ring.record("PROCESS_EXIT", {"exit_code": 1}, source="test", status="error")
    w_ring.close()

    server = BlackboxMCPServer(db_path=str(wal_path))
    try:
        tools_resp = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tool_names = [t["name"] for t in tools_resp["result"]["tools"]]
        assert len(tool_names) == 14
        assert "diagnose_performance_and_flakiness" in tool_names

        perf_resp = server.handle_request({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "diagnose_performance_and_flakiness",
                "arguments": {
                    "db_path": str(wal_path),
                    "baseline_db_path": str(baseline_path),
                    "n_plus_one_threshold": 5,
                },
            },
        })
        perf_payload = json.loads(perf_resp["result"]["content"][0]["text"])
        assert len(perf_payload["n_plus_one_queries"]) == 1
        assert len(perf_payload["latency_regressions"]) == 1

        ctx_resp = server.handle_request({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "get_full_incident_context",
                "arguments": {
                    "db_path": str(wal_path),
                    "baseline_db_path": str(baseline_path),
                    "root_dir": str(tmp_path),
                },
            },
        })
        ctx_payload = json.loads(ctx_resp["result"]["content"][0]["text"])
        assert "performance_and_flakiness" in ctx_payload
        assert len(ctx_payload["performance_and_flakiness"]["n_plus_one_queries"]) == 1
        assert len(ctx_payload["performance_and_flakiness"]["latency_regressions"]) == 1
    finally:
        server.close()

