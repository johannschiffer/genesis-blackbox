"""
Unit and integration tests for Genesis Blackbox Phase 8:
- CausalVerifier automated test execution and .blackbox-resolved.md receipt generation
- CLI blackbox verify subcommand (exit code 0 and 1)
- MCP tool verify_crash_resolution
- Native integrations: instrument_fastapi, instrument_flask, instrument_engine
"""

import os
import sys
import json
import sqlite3
import pytest
from unittest.mock import patch, MagicMock

from blackbox.recorder.wal_ring import WalRingBuffer
from blackbox.recorder.exception_hook import ExceptionHook
from blackbox.compiler.reproducer import CrashReproducer
from blackbox.compiler.verifier import CausalVerifier
from blackbox.mcp_server import BlackboxMCPServer
from blackbox.cli import main as cli_main
from blackbox.integrations.fastapi import instrument_fastapi
from blackbox.integrations.flask import instrument_flask
from blackbox.integrations.sqlalchemy import instrument_engine


def test_causal_verifier_flow_failure_and_resolution(tmp_path):
    """
    Validates end-to-end bug reproduction and autonomous resolution loop:
    1. A broken script is created and executed, failing with an exception recorded in WAL.
    2. A reproduction test is synthesized via CrashReproducer.
    3. CausalVerifier is run against broken script -> reports resolved: False.
    4. The script is fixed.
    5. CausalVerifier is re-run -> reports resolved: True and issues .blackbox-resolved.md.
    """
    db_file = str(tmp_path / "wal.sqlite")
    receipt_file = str(tmp_path / ".blackbox-resolved.md")
    ring = WalRingBuffer(db_path=db_file)
    hook = ExceptionHook(wal_ring=ring)

    # 1. Create a dummy service file with a bug
    service_file = tmp_path / "service.py"
    broken_code = """
def calculate_tax(amount: float, rate: float) -> float:
    if rate < 0 or rate > 1:
        raise ValueError(f"Invalid tax rate: {rate}")
    return amount * rate
"""
    service_file.write_text(broken_code, encoding="utf-8")

    # Import and execute the broken function to record exception into WAL
    sys.path.insert(0, str(tmp_path))
    try:
        import service
        try:
            service.calculate_tax(100.0, -0.05)
        except ValueError:
            exc_type, exc_val, tb = sys.exc_info()
            hook.handle_exception(exc_type, exc_val, tb)
    finally:
        if str(tmp_path) in sys.path:
            sys.path.remove(str(tmp_path))

    # 2. Synthesize reproduction test
    reproducer = CrashReproducer(wal_ring=ring)
    reproduce_code = reproducer.generate()
    test_reproduce_file = str(tmp_path / "test_reproduce.py")
    with open(test_reproduce_file, "w", encoding="utf-8") as f:
        f.write(reproduce_code)

    # 3. Verify on UNFIXED code -> must report resolved: False
    verifier = CausalVerifier(wal_ring=ring, db_path=db_file, receipt_path=receipt_file)
    res_broken = verifier.verify(test_path=test_reproduce_file)

    assert res_broken["resolved"] is False
    assert res_broken["exit_code"] != 0
    assert not os.path.isfile(receipt_file)

    # 4. Apply fix to service.py (handle negative rate safely by clamping or default)
    fixed_code = """
def calculate_tax(amount: float, rate: float) -> float:
    # Fix: Clamped valid tax rate without raising ValueError
    safe_rate = max(0.0, min(rate, 1.0))
    return amount * safe_rate
"""
    service_file.write_text(fixed_code, encoding="utf-8")

    # 5. Verify on FIXED code -> must report resolved: True and issue receipt
    res_fixed = verifier.verify(test_path=test_reproduce_file)

    assert res_fixed["resolved"] is True
    assert res_fixed["exit_code"] == 0
    assert res_fixed["receipt_path"] == receipt_file
    assert os.path.isfile(receipt_file)

    # 6. Verify receipt contents
    with open(receipt_file, "r", encoding="utf-8") as rf:
        receipt_text = rf.read()

    assert "# BLACKBOX CRASH RESOLUTION VERIFICATION RECEIPT" in receipt_text
    assert "VERIFIED_RESOLVED (PASS)" in receipt_text
    assert "ValueError" in receipt_text
    assert "bb-proof-" in receipt_text


def test_cli_verify_subcommand(tmp_path):
    """Validates CLI invocation of blackbox verify command with exit codes 0 and 1."""
    db_file = str(tmp_path / "wal.sqlite")
    receipt_file = str(tmp_path / ".blackbox-resolved.md")
    ring = WalRingBuffer(db_path=db_file)

    # Failing test file
    fail_test = str(tmp_path / "test_fail.py")
    with open(fail_test, "w", encoding="utf-8") as f:
        f.write("def test_broken():\n    assert False\n")

    # CLI exit code 1 on failure
    ret_fail = cli_main(["verify", "--test", fail_test, "--receipt", receipt_file, "--db", db_file])
    assert ret_fail == 1
    assert not os.path.isfile(receipt_file)

    # Passing test file
    pass_test = str(tmp_path / "test_pass.py")
    with open(pass_test, "w", encoding="utf-8") as f:
        f.write("def test_ok():\n    assert True\n")

    # CLI exit code 0 on success
    ret_pass = cli_main(["verify", "--test", pass_test, "--receipt", receipt_file, "--db", db_file])
    assert ret_pass == 0
    assert os.path.isfile(receipt_file)


def test_mcp_verify_crash_resolution_tool(tmp_path):
    """Validates MCP tool verify_crash_resolution."""
    db_file = str(tmp_path / "wal.sqlite")
    receipt_file = str(tmp_path / ".blackbox-resolved.md")
    server = BlackboxMCPServer(db_path=db_file)

    # Verify tool in tools/list
    list_resp = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = [t["name"] for t in list_resp["result"]["tools"]]
    assert "verify_crash_resolution" in tools

    # Create dummy passing test
    pass_test = str(tmp_path / "test_pass.py")
    with open(pass_test, "w", encoding="utf-8") as f:
        f.write("def test_mcp_ok():\n    assert True\n")

    # Call verify_crash_resolution via tools/call
    call_resp = server.handle_request({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "verify_crash_resolution",
            "arguments": {
                "test_path": pass_test,
                "db_path": db_file,
                "receipt_path": receipt_file
            }
        }
    })

    assert "result" in call_resp
    text_content = call_resp["result"]["content"][0]["text"]
    data = json.loads(text_content)
    assert data["resolved"] is True
    assert data["exit_code"] == 0
    assert os.path.isfile(receipt_file)


def test_framework_integrations(tmp_path):
    """Validates FastAPI, Flask, and SQLAlchemy integration wrappers."""
    db_file = str(tmp_path / "wal.sqlite")

    # 1. FastAPI Integration
    class MockFastAPI:
        def __init__(self):
            self.middlewares = []

        def add_middleware(self, mw, **kw):
            self.middlewares.append((mw, kw))

    fastapi_app = MockFastAPI()
    res_fastapi = instrument_fastapi(fastapi_app, wal_path=db_file)
    assert res_fastapi is fastapi_app
    assert len(fastapi_app.middlewares) == 1

    # 2. Flask Integration
    class MockFlask:
        def __init__(self):
            self.wsgi_app = lambda env, sr: [b"flask"]

        def teardown_request(self, fn):
            return fn

    flask_app = MockFlask()
    res_flask = instrument_flask(flask_app, wal_path=db_file)
    assert res_flask is flask_app
    assert hasattr(flask_app.wsgi_app, "wal_ring")

    # 3. SQLAlchemy Integration
    class MockDialect:
        name = "sqlite"

    class MockEngine:
        dialect = MockDialect()
        _has_events = True

    engine = MockEngine()
    res_engine = instrument_engine(engine, wal_path=db_file)
    assert res_engine is engine


def test_causal_verifier_missing_test_file(tmp_path):
    """Validates CausalVerifier response when the target test file does not exist."""
    db_file = str(tmp_path / "wal.sqlite")
    verifier = CausalVerifier(db_path=db_file)
    res = verifier.verify(test_path=str(tmp_path / "non_existent_test.py"))
    assert res["resolved"] is False
    assert res["exit_code"] == 1
    assert "not found" in res["output"]


def test_framework_raw_callables(tmp_path):
    """Validates framework instrumentors on raw callable functions."""
    db_file = str(tmp_path / "wal.sqlite")

    # Raw ASGI callable
    async def raw_asgi(scope, receive, send):
        pass

    wrapped_asgi = instrument_fastapi(raw_asgi, wal_path=db_file)
    assert hasattr(wrapped_asgi, "wal_ring")

    # Raw WSGI callable
    def raw_wsgi(environ, start_response):
        return [b"hello"]

    wrapped_wsgi = instrument_flask(raw_wsgi, wal_path=db_file)
    assert hasattr(wrapped_wsgi, "wal_ring")

