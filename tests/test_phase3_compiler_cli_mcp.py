"""
Unit and integration tests for Genesis Blackbox Phase 3:
Causal Crash Compiler, CLI Subcommands, and Native MCP Server.
"""

import os
import sys
import tempfile
import pytest
from blackbox.recorder.wal_ring import WalRingBuffer
from blackbox.recorder.exception_hook import ExceptionHook
from blackbox.ast_engine.graph import CodebaseGraph
from blackbox.compiler.autopsy import CausalCrashCompiler
from blackbox.cli import main as cli_main
from blackbox.mcp_server import BlackboxMCPServer


@pytest.fixture
def temp_workspace():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_wal.sqlite")
        sample_code = """
def lookup_rate(country):
    return 0.21

def calculate_tax(amount, country):
    rate = lookup_rate(country)
    secret_key = "sk_live_12345678"
    if amount < 0:
        raise ValueError(f"Negative amount: {amount}")
    return amount * rate

def process_payment(amount, country):
    return calculate_tax(amount, country)
"""
        code_file = os.path.join(tmpdir, "finance_module.py")
        with open(code_file, "w", encoding="utf-8") as f:
            f.write(sample_code)

        yield tmpdir, db_path, code_file


def test_end_to_end_autopsy_capsule_generation(temp_workspace):
    tmpdir, db_path, code_file = temp_workspace
    ring = WalRingBuffer(db_path=db_path)

    # 1. Record preceding HTTP transaction
    ring.record(
        event_type="HTTP_REQ",
        payload={
            "method": "POST",
            "path": "/api/v1/checkout",
            "status_code": 500,
            "query": "currency=EUR",
            "headers": {"content-type": "application/json"}
        },
        source="asgi_middleware",
        duration_ms=48.5,
        status="error"
    )

    # 2. Record preceding SQL query
    ring.record(
        event_type="SQL_QUERY",
        payload={
            "query": "SELECT balance, rate FROM accounts WHERE account_id = ?",
            "params": [42],
            "rows_affected": 1
        },
        source="sql_interceptor",
        duration_ms=1.35,
        status="ok"
    )

    # 3. Simulate and serialize exception in the sample code
    graph = CodebaseGraph()
    graph.parse_file(code_file)
    hook = ExceptionHook(wal_ring=ring)

    try:
        # Simulate execution of the function
        amount = -50.0
        country = "US"
        token = "secret_auth_token_xyz"
        if amount < 0:
            raise ValueError("Amount cannot be negative")
    except ValueError as e:
        exc_type, exc_val, tb = sys.exc_info()
        hook.handle_exception(exc_type, exc_val, tb)

    # 4. Compile Autopsy Capsule
    compiler = CausalCrashCompiler(wal_ring=ring, ast_graph=graph)
    capsule = compiler.compile()

    # 5. Verify capsule sections and content
    assert "BLACKBOX CRASH AUTOPSY CAPSULE" in capsule
    assert "ValueError" in capsule
    assert "Amount cannot be negative" in capsule
    assert "Recent HTTP Transactions" in capsule
    assert "/api/v1/checkout" in capsule
    assert "Preceding SQL Queries" in capsule
    assert "SELECT balance, rate FROM accounts" in capsule
    assert "Local Variable State at Crash Time" in capsule
    assert "Suggested Fix Mandate" in capsule


def test_cli_subcommands(temp_workspace, capsys):
    tmpdir, db_path, code_file = temp_workspace

    # 1. Test CLI 'record' with manual payload
    ret = cli_main(["record", "--db", db_path, "--type", "AUDIT_LOG", "--payload", '{"action": "user_login"}'])
    assert ret == 0

    # 2. Test CLI 'record' executing a python script command
    run_script = os.path.join(tmpdir, "simple_run.py")
    with open(run_script, "w") as f:
        f.write("import sys\nprint('Script executed successfully')\nsys.exit(0)\n")
    ret_run = cli_main(["record", "--db", db_path, "--", sys.executable, run_script])
    assert ret_run == 0

    # 3. Test CLI 'timeline'
    ret_time = cli_main(["timeline", "--db", db_path, "--limit", "10"])
    assert ret_time == 0
    captured = capsys.readouterr()
    assert "Flight Recorder Timeline" in captured.out

    # 4. Test CLI 'blast-radius'
    ret_blast = cli_main(["blast-radius", code_file, "--symbol", "calculate_tax"])
    assert ret_blast == 0
    captured_blast = capsys.readouterr()
    assert '"target": "calculate_tax"' in captured_blast.out
    assert "process_payment" in captured_blast.out
    assert "lookup_rate" in captured_blast.out

    # 5. Test CLI 'autopsy'
    out_file = os.path.join(tmpdir, "output_autopsy.md")
    ret_auto = cli_main(["autopsy", "--db", db_path, "--output", out_file])
    assert ret_auto == 0
    assert os.path.isfile(out_file)
    with open(out_file, "r") as f:
        content = f.read()
    assert len(content) > 0


def test_mcp_server_native_tools(temp_workspace):
    tmpdir, db_path, code_file = temp_workspace
    server = BlackboxMCPServer(db_path=db_path)

    # 1. Test record_event tool
    rec_res = server.record_event(
        event_type="SQL_QUERY",
        payload={"query": "INSERT INTO logs VALUES (?)", "params": ["entry_1"]},
        source="mcp_test"
    )
    assert rec_res["status"] == "recorded"
    assert rec_res["event_id"] > 0

    # 2. Test query_execution_timeline tool
    timeline = server.query_execution_timeline(limit=10, event_type="SQL_QUERY")
    assert timeline["total_events"] >= 1
    assert timeline["events"][0]["event_type"] == "SQL_QUERY"

    # 3. Test get_blast_radius tool
    blast = server.get_blast_radius(file_path=code_file, symbol="calculate_tax")
    assert blast["target"] == "calculate_tax"
    assert "process_payment" in blast["upstream_callers"]
    assert "lookup_rate" in blast["downstream_callees"]

    # 4. Test get_crash_autopsy tool
    autopsy = server.get_crash_autopsy(db_path=db_path)
    assert isinstance(autopsy, str)
    assert len(autopsy) > 0

    # 5. Test JSON-RPC protocol compliance
    init_req = {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
    init_resp = server.handle_request(init_req)
    assert init_resp["result"]["serverInfo"]["name"] == "genesis-blackbox"

    list_req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    list_resp = server.handle_request(list_req)
    tool_names = [t["name"] for t in list_resp["result"]["tools"]]
    assert "get_crash_autopsy" in tool_names
    assert "get_blast_radius" in tool_names
    assert "query_execution_timeline" in tool_names

    call_req = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "get_blast_radius",
            "arguments": {"file_path": code_file, "symbol": "calculate_tax"}
        }
    }
    call_resp = server.handle_request(call_req)
    assert "result" in call_resp
    assert "content" in call_resp["result"]
    assert "calculate_tax" in call_resp["result"]["content"][0]["text"]
