"""
Genesis Blackbox v0.2.0 Test Suite
Covers:
- Pillar 1: Polyglot AST Engine, TS/JS Blast-Radius, Next.js/React boundaries, and Node/V8 crash autopsy ingestion
- Pillar 2: StateDiffer time-travel execution diffing, SQL/HTTP/variable divergence, CLI diff-runs, and MCP integration
"""

import os
import sys
import json
import subprocess
from pathlib import Path

from blackbox import (
    WalRingBuffer,
    CodebaseGraph,
    PolyglotParser,
    parse_v8_stack_trace,
    ImpactTestSelector,
    CausalCrashCompiler,
    StateDiffer,
    BlackboxMCPServer,
)
from blackbox.recorder.runner import run_target
from blackbox.cli import main as cli_main


def test_polyglot_parser_syntax_and_boundaries(tmp_path: Path):
    """Verifies pure-Python TS/JS parser extracts imports, exports, functions, and Next.js/React boundaries."""
    ts_code = '''
    import { useState, useEffect } from "react";
    import axios from "axios";
    const { formatCurrency } = require("../utils/format");

    export interface OrderProps {
        orderId: string;
        amount: number;
    }

    export const calculateTax = (amount: number, rate: number = 0.08): number => {
        return amount * rate;
    };

    export async function processOrder(orderId: string, amount: number) {
        const tax = calculateTax(amount);
        const formatted = formatCurrency(amount + tax);
        await axios.post("/api/orders", { orderId, total: formatted });
        return { orderId, tax, total: formatted };
    }

    export default function OrderSummary({ orderId, amount }: OrderProps) {
        const [status, setStatus] = useState("pending");
        useEffect(() => {
            processOrder(orderId, amount);
        }, [orderId]);

        return (
            <div className="order-box">
                <h1>Order {orderId}</h1>
            </div>
        );
    }
    '''

    src_file = tmp_path / "OrderSummary.tsx"
    src_file.write_text(ts_code, encoding="utf-8")

    parser = PolyglotParser(str(src_file))
    result = parser.parse(ts_code)

    # 1. Imports
    assert "react" in result["imported_modules"]
    assert "axios" in result["imported_modules"]
    assert "../utils/format" in result["imported_modules"]
    assert "useState" in result["imported_symbols"]
    assert "useEffect" in result["imported_symbols"]
    assert "formatCurrency" in result["imported_symbols"]

    # 2. Exports
    exported_names = [e["name"] for e in result["exports"]]
    assert "calculateTax" in exported_names
    assert "processOrder" in exported_names
    assert "OrderSummary" in exported_names

    # 3. Functions
    funcs = result["functions"]
    assert "calculateTax" in funcs
    assert funcs["calculateTax"]["args"] == ["amount", "rate"]

    assert "processOrder" in funcs
    assert funcs["processOrder"]["args"] == ["orderId", "amount"]
    assert "calculateTax" in funcs["processOrder"]["calls"]
    assert "formatCurrency" in funcs["processOrder"]["calls"]
    assert "post" in funcs["processOrder"]["calls"]

    # 4. React Component Boundary
    assert "OrderSummary" in funcs
    assert funcs["OrderSummary"]["is_component"] is True
    assert "useState" in funcs["OrderSummary"]["calls"]
    assert "processOrder" in funcs["OrderSummary"]["calls"]


def test_nextjs_route_handler_detection(tmp_path: Path):
    """Verifies App Router route handler boundary detection (GET, POST, etc.)."""
    route_code = '''
    import { NextResponse } from "next/server";
    import { processOrder } from "../../services/order";

    export async function GET(request: Request) {
        return NextResponse.json({ status: "alive" });
    }

    export async function POST(request: Request) {
        const data = await request.json();
        const res = await processOrder(data.orderId, data.amount);
        return NextResponse.json(res);
    }
    '''

    route_file = tmp_path / "route.ts"
    route_file.write_text(route_code, encoding="utf-8")

    parser = PolyglotParser(str(route_file))
    res = parser.parse(route_code)
    funcs = res["functions"]

    assert "GET" in funcs
    assert funcs["GET"]["is_route_handler"] is True

    assert "POST" in funcs
    assert funcs["POST"]["is_route_handler"] is True
    assert "processOrder" in funcs["POST"]["calls"]


def test_polyglot_codebase_graph_blast_radius(tmp_path: Path):
    """Verifies CodebaseGraph builds cross-file call graphs and upstream blast radius for TS/JS files."""
    # File 1: Math utility
    math_ts = tmp_path / "math.ts"
    math_ts.write_text(
        "export function calculateDiscount(price: number, percent: number) {\n"
        "    return price * (1 - percent);\n"
        "}\n",
        encoding="utf-8"
    )

    # File 2: Order service calling math
    order_ts = tmp_path / "order.ts"
    order_ts.write_text(
        "import { calculateDiscount } from './math';\n"
        "export function createInvoice(price: number, discount: number) {\n"
        "    const finalPrice = calculateDiscount(price, discount);\n"
        "    return { price, finalPrice };\n"
        "}\n",
        encoding="utf-8"
    )

    # File 3: Checkout component calling order service
    checkout_tsx = tmp_path / "Checkout.tsx"
    checkout_tsx.write_text(
        "import { createInvoice } from './order';\n"
        "export function CheckoutPage() {\n"
        "    const inv = createInvoice(100, 0.1);\n"
        "    return <div>{inv.finalPrice}</div>;\n"
        "}\n",
        encoding="utf-8"
    )

    graph = CodebaseGraph()
    count = graph.parse_directory(str(tmp_path))
    assert count >= 3

    # Upstream blast radius of calculateDiscount
    radius = graph.get_blast_radius("calculateDiscount", depth=3)
    assert radius["target"] == "calculateDiscount"
    assert "createInvoice" in radius["upstream_callers"]
    assert "CheckoutPage" in radius["upstream_callers"]

    # Downstream callees of CheckoutPage
    down_radius = graph.get_blast_radius("CheckoutPage", depth=2)
    assert "createInvoice" in down_radius["downstream_callees"]
    assert "calculateDiscount" in down_radius["downstream_callees"]


def test_polyglot_impact_test_selector(tmp_path: Path):
    """Verifies ImpactTestSelector maps modified TypeScript source files to impacted test files."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    service_file = src_dir / "calculator.ts"
    service_file.write_text(
        "export function add(a: number, b: number) { return a + b; }\n"
        "export function sub(a: number, b: number) { return a - b; }\n",
        encoding="utf-8"
    )

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    test_file = tests_dir / "calculator.test.ts"
    test_file.write_text(
        "import { add } from '../src/calculator';\n"
        "describe('calc', () => {\n"
        "    it('adds', () => {\n"
        "        expect(add(1, 2)).toBe(3);\n"
        "    });\n"
        "});\n",
        encoding="utf-8"
    )

    selector = ImpactTestSelector(root_dir=str(tmp_path))
    impacted = selector.find_impacted_tests(files=[str(service_file)], diff=False)

    assert len(impacted) == 1
    assert os.path.normpath(impacted[0]) == os.path.normpath(str(test_file))


def test_v8_stack_trace_parser_and_autopsy_capsule(tmp_path: Path):
    """Verifies parsing of real V8 stack trace and compilation into .blackbox.md autopsy."""
    dummy_ts = tmp_path / "orderService.ts"
    dummy_ts.write_text(
        "export function processPayment(user: any) {\n"
        "    const token = user.card.token;\n"
        "    return token;\n"
        "}\n",
        encoding="utf-8"
    )

    v8_stderr = f"""
{dummy_ts}:2
    const token = user.card.token;
                            ^

TypeError: Cannot read properties of undefined (reading 'token')
    at processPayment ({dummy_ts}:2:29)
    at handleCheckout (/app/src/checkout.js:15:9)
    at Module._compile (node:internal/modules/cjs/loader:1100:14)
"""

    parsed = parse_v8_stack_trace(v8_stderr)
    assert parsed is not None
    assert parsed["exception_type"] == "TypeError"
    assert "token" in parsed["exception_message"]
    assert parsed["runtime"] == "node"

    rc = parsed["root_cause"]
    assert rc["function"] == "processPayment"
    assert rc["lineno"] == 2
    assert "token" in rc["code_line"]
    assert rc["locals"].get("token") is None

    # Ingestion into WAL and autopsy compilation
    db_file = tmp_path / "v8_wal.sqlite"
    ring = WalRingBuffer(db_path=str(db_file))
    try:
        ring.record(event_type="EXCEPTION", payload=parsed, source="node_runner", status="error")
        graph = CodebaseGraph()
        graph.parse_file(str(dummy_ts))

        compiler = CausalCrashCompiler(wal_ring=ring, ast_graph=graph)
        capsule = compiler.compile()

        assert "BLACKBOX CRASH AUTOPSY CAPSULE" in capsule
        assert "`TypeError`: Cannot read properties of undefined (reading 'token')" in capsule
        assert str(dummy_ts) in capsule
        assert "processPayment()" in capsule
        assert "```typescript" in capsule
    finally:
        ring.close()


def test_runner_subprocess_node_captures_v8_crash(tmp_path: Path):
    """Verifies that running a crashing Node.js script captures the V8 exception into wal.sqlite."""
    node_bin = "/usr/bin/node"
    if not os.path.isfile(node_bin):
        return

    crash_js = tmp_path / "node_crash.js"
    crash_js.write_text(
        "function boom(obj) {\n"
        "    return obj.nested.value;\n"
        "}\n"
        "boom(undefined);\n",
        encoding="utf-8"
    )

    db_file = tmp_path / "node_wal.sqlite"
    rc = run_target([node_bin, str(crash_js)], db_path=str(db_file))
    assert rc != 0

    ring = WalRingBuffer(db_path=str(db_file))
    try:
        exceptions = ring.query_recent(event_type="EXCEPTION")
        assert len(exceptions) >= 1
        exc_payload = exceptions[0]["payload"]
        assert exc_payload["exception_type"] == "TypeError"
        assert exc_payload["runtime"] == "node"
        assert exc_payload["root_cause"]["function"] == "boom"
    finally:
        ring.close()


def test_state_differ_exact_sql_and_variable_divergence(tmp_path: Path):
    """Verifies StateDiffer isolates the exact SQL query failure, bind param mutation, and variable state."""
    db_file = tmp_path / "differ_wal.sqlite"
    ring = WalRingBuffer(db_path=str(db_file))

    try:
        # RUN 1: Healthy baseline execution (PASSING)
        ring.record(
            event_type="PROCESS_START",
            payload={"run_id": "run-baseline-01", "cmd": "service_worker.py"},
            source="runner"
        )
        ring.record(
            event_type="HTTP_REQ",
            payload={"method": "GET", "path": "/api/users/42", "status_code": 200},
            source="http"
        )
        ring.record(
            event_type="SQL_QUERY",
            payload={"query": "SELECT * FROM users WHERE id = ?", "params": [42], "rows_affected": 1},
            status="ok",
            source="db"
        )
        ring.record(
            event_type="SQL_QUERY",
            payload={"query": "SELECT * FROM orders WHERE user_id = ?", "params": [42], "rows_affected": 3},
            status="ok",
            source="db"
        )
        ring.record(
            event_type="PROCESS_EXIT",
            payload={"run_id": "run-baseline-01", "exit_code": 0},
            status="ok",
            source="runner"
        )

        # RUN 2: Failing execution (CRASHING)
        ring.record(
            event_type="PROCESS_START",
            payload={"run_id": "run-failing-02", "cmd": "service_worker.py"},
            source="runner"
        )
        ring.record(
            event_type="HTTP_REQ",
            payload={"method": "GET", "path": "/api/users/42", "status_code": 200},
            source="http"
        )
        ring.record(
            event_type="SQL_QUERY",
            payload={"query": "SELECT * FROM users WHERE id = ?", "params": [42], "rows_affected": 1},
            status="ok",
            source="db"
        )
        # Divergence point: Query fails with table not found and param mutated to None
        ring.record(
            event_type="SQL_QUERY",
            payload={
                "query": "SELECT * FROM orders_v2 WHERE user_id = ?",
                "params": [None],
                "error": "no such table: orders_v2"
            },
            status="error",
            source="db"
        )
        ring.record(
            event_type="EXCEPTION",
            payload={
                "exception_type": "DatabaseError",
                "exception_message": "no such table: orders_v2",
                "root_cause": {
                    "filename": "/app/services/orders.py",
                    "lineno": 58,
                    "function": "fetch_user_orders",
                    "code_line": "cursor.execute(sql, (user_id,))",
                    "locals": {"user_id": None, "sql": "SELECT * FROM orders_v2 WHERE user_id = ?"}
                }
            },
            status="error",
            source="runner"
        )
        ring.record(
            event_type="PROCESS_EXIT",
            payload={"run_id": "run-failing-02", "exit_code": 1},
            status="error",
            source="runner"
        )

        differ = StateDiffer(wal_ring=ring)
        diff_res = differ.diff(baseline_run_id="run-baseline-01", failing_run_id="run-failing-02")

        # Verify runs detected
        assert diff_res["baseline_run"]["run_id"] == "run-baseline-01"
        assert diff_res["baseline_run"]["status"] == "passing"
        assert diff_res["failing_run"]["run_id"] == "run-failing-02"
        assert diff_res["failing_run"]["status"] == "failing"

        # Verify exact divergence point identified
        div = diff_res["divergence_point"]
        assert div is not None
        assert div["step"] == 4
        assert div["type"] == "SQL_QUERY_FAILURE"
        assert "failed in crashing run" in div["description"]

        # Verify SQL diff details
        sql_diff = diff_res["sql_diff"]
        assert len(sql_diff) == 2
        assert sql_diff[1]["divergence"] is not None
        assert "no such table: orders_v2" in sql_diff[1]["divergence"]

        # Verify variable mutation detection
        var_diff = diff_res["variable_diff"]
        assert var_diff["exception"]["type"] == "DatabaseError"
        assert "user_id" in var_diff["mutated_variables"]
        assert var_diff["mutated_variables"]["user_id"]["issue"] == "Value mutated to None"

        # Verify markdown report formatting
        md_report = differ.compile_markdown(diff_res)
        assert "# ⏱️ BLACKBOX TIME-TRAVEL EXECUTION DIFF" in md_report
        assert "Step #4 (`SQL_QUERY_FAILURE`)" in md_report
        assert "Value mutated to None" in md_report

    finally:
        ring.close()


def test_cli_diff_runs_subcommand(tmp_path: Path, monkeypatch, capsys):
    """Verifies blackbox diff-runs CLI command outputs markdown and saves report."""
    db_file = tmp_path / "cli_diff_wal.sqlite"
    ring = WalRingBuffer(db_path=str(db_file))
    try:
        # Run 1
        ring.record("PROCESS_START", {"run_id": "run-1"})
        ring.record("HTTP_REQ", {"path": "/ping", "status_code": 200}, status="ok")
        ring.record("PROCESS_EXIT", {"exit_code": 0})
        # Run 2
        ring.record("PROCESS_START", {"run_id": "run-2"})
        ring.record("HTTP_REQ", {"path": "/ping", "status_code": 500}, status="error")
        ring.record("PROCESS_EXIT", {"exit_code": 1}, status="error")
    finally:
        ring.close()

    out_md = tmp_path / ".blackbox-diff.md"
    ret = cli_main(["diff-runs", "--db", str(db_file), "--output", str(out_md)])
    assert ret == 0
    assert out_md.is_file()
    content = out_md.read_text(encoding="utf-8")
    assert "BLACKBOX TIME-TRAVEL EXECUTION DIFF" in content
    assert "HTTP_STATUS_FAILURE" in content


def test_mcp_compare_execution_runs_tool(tmp_path: Path):
    """Verifies MCP server compare_execution_runs native tool returns structured divergence."""
    db_file = tmp_path / "mcp_diff_wal.sqlite"
    ring = WalRingBuffer(db_path=str(db_file))
    try:
        ring.record("PROCESS_START", {"run_id": "p1"})
        ring.record("SQL_QUERY", {"query": "SELECT 1", "params": [1]}, status="ok")
        ring.record("PROCESS_EXIT", {"exit_code": 0})

        ring.record("PROCESS_START", {"run_id": "f2"})
        ring.record("SQL_QUERY", {"query": "SELECT 1", "params": [None]}, status="error")
        ring.record("PROCESS_EXIT", {"exit_code": 1}, status="error")
    finally:
        ring.close()

    mcp = BlackboxMCPServer(db_path=str(db_file))
    try:
        tool_fn = mcp.tools["compare_execution_runs"]["handler"]
        json_report = tool_fn({"db_path": str(db_file), "format": "json"})
        assert isinstance(json_report, dict)
        assert json_report["divergence_point"]["type"] == "SQL_QUERY_FAILURE"
        assert json_report["baseline_run"]["status"] == "passing"
        assert json_report["failing_run"]["status"] == "failing"
    finally:
        mcp.wal_ring.close()


def test_cli_blast_radius_typescript_file(tmp_path: Path, capsys):
    """Verifies CLI blast-radius command on a TypeScript file."""
    ts_file = tmp_path / "service.ts"
    ts_file.write_text(
        "export function helper(x: number) { return x * 2; }\n"
        "export function mainTask(y: number) { return helper(y); }\n",
        encoding="utf-8"
    )

    ret = cli_main(["blast-radius", str(ts_file), "--symbol", "helper"])
    assert ret == 0
    captured = capsys.readouterr()
    res = json.loads(captured.out)
    assert res["target"] == "helper"
    assert "mainTask" in res["upstream_callers"]


def test_state_differ_http_schema_shift(tmp_path: Path):
    """Verifies StateDiffer isolates HTTP payload schema divergence (missing fields)."""
    db_file = tmp_path / "http_schema_wal.sqlite"
    ring = WalRingBuffer(db_path=str(db_file))
    try:
        # Run 1: Healthy
        ring.record("PROCESS_START", {"run_id": "api-1"})
        ring.record(
            "HTTP_REQ",
            {"method": "POST", "path": "/checkout", "body": {"user_id": 42, "coupon": "DISCOUNT10", "items": [1]}},
            status="ok"
        )
        ring.record("PROCESS_EXIT", {"exit_code": 0})

        # Run 2: Failing (coupon key missing)
        ring.record("PROCESS_START", {"run_id": "api-2"})
        ring.record(
            "HTTP_REQ",
            {"method": "POST", "path": "/checkout", "body": {"user_id": 42, "items": [1]}},
            status="error"
        )
        ring.record("PROCESS_EXIT", {"exit_code": 1}, status="error")
    finally:
        ring.close()

    differ = StateDiffer(db_path=str(db_file))
    try:
        diff_res = differ.diff(baseline_run_id="api-1", failing_run_id="api-2")
        assert diff_res["divergence_point"]["type"] == "HTTP_STATUS_FAILURE"
        assert len(diff_res["http_diff"]) == 1
        assert "coupon" in diff_res["http_diff"][0]["divergence"]
    finally:
        differ.close()


def test_state_differ_missing_baseline_raises(tmp_path: Path):
    """Verifies StateDiffer raises ValueError when no passing baseline is found."""
    import pytest
    db_file = tmp_path / "only_failing_wal.sqlite"
    ring = WalRingBuffer(db_path=str(db_file))
    try:
        ring.record("PROCESS_START", {"run_id": "f1"})
        ring.record("EXCEPTION", {"exception_type": "Crash"}, status="error")
        ring.record("PROCESS_EXIT", {"exit_code": 1}, status="error")
    finally:
        ring.close()

    differ = StateDiffer(db_path=str(db_file))
    try:
        with pytest.raises(ValueError, match="Could not find a baseline PASSING execution run"):
            differ.diff()
    finally:
        differ.close()

