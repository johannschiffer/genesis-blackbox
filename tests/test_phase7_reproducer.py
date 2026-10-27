"""
Unit and integration tests for Genesis Blackbox Phase 7:
- CrashReproducer code synthesis from WAL exceptions
- Syntax validation via ast.parse
- CLI subcommands (blackbox reproduce, blackbox init-rules)
- MCP server tool generate_reproduction_test
"""

import os
import sys
import ast
import json
import pytest
from unittest.mock import patch

from blackbox.recorder.wal_ring import WalRingBuffer
from blackbox.recorder.exception_hook import ExceptionHook
from blackbox.compiler.reproducer import CrashReproducer
from blackbox.mcp_server import BlackboxMCPServer
from blackbox.cli import main as cli_main


def helper_sample_crash_function(x: int, y: str):
    """Sample helper to trigger a realistic runtime crash with local variables."""
    ratio = x * 2
    tag = y.upper()
    if ratio > 10:
        raise ValueError(f"Ratio {ratio} exceeded threshold with tag {tag}")
    return ratio


def test_reproducer_synthesis_and_ast_compilation(tmp_path):
    """Validates that CrashReproducer synthesizes valid, compile-able Python code."""
    db_file = str(tmp_path / "wal.sqlite")
    ring = WalRingBuffer(db_path=db_file)
    hook = ExceptionHook(wal_ring=ring)

    # Trigger a realistic crash and serialize into WAL
    try:
        helper_sample_crash_function(10, "high_load")
    except ValueError:
        exc_type, exc_val, tb = sys.exc_info()
        hook.handle_exception(exc_type, exc_val, tb)

    # Synthesize reproduction test
    reproducer = CrashReproducer(wal_ring=ring)
    code = reproducer.generate()

    # 1. Validate non-empty output
    assert code is not None and len(code) > 0
    assert "Genesis Blackbox: Deterministic Crash Reproduction Test" in code
    assert "ValueError" in code
    assert "ratio" in code or "helper_sample_crash_function" in code

    # 2. Validate AST syntax compilation without SyntaxError
    parsed_ast = ast.parse(code)
    assert isinstance(parsed_ast, ast.Module)


def test_reproducer_fallback_when_no_exception(tmp_path):
    """Validates fallback template when no exception exists in WAL."""
    db_file = str(tmp_path / "empty_wal.sqlite")
    ring = WalRingBuffer(db_path=db_file)

    reproducer = CrashReproducer(wal_ring=ring)
    code = reproducer.generate()

    assert "No EXCEPTION event recorded" in code
    parsed_ast = ast.parse(code)
    assert isinstance(parsed_ast, ast.Module)


def test_cli_reproduce_subcommand(tmp_path):
    """Validates CLI invocation of blackbox reproduce command."""
    db_file = str(tmp_path / "wal.sqlite")
    ring = WalRingBuffer(db_path=db_file)
    hook = ExceptionHook(wal_ring=ring)

    try:
        helper_sample_crash_function(20, "cli_test")
    except ValueError:
        exc_type, exc_val, tb = sys.exc_info()
        hook.handle_exception(exc_type, exc_val, tb)

    output_script = str(tmp_path / "test_reproduce.py")
    ret = cli_main(["reproduce", "--db", db_file, "--output", output_script])
    assert ret == 0
    assert os.path.isfile(output_script)

    with open(output_script, "r", encoding="utf-8") as f:
        content = f.read()

    assert "def test_reproduce_and_verify_fix():" in content
    ast.parse(content)


def test_cli_init_rules_subcommand(tmp_path):
    """Validates CLI invocation of blackbox init-rules command."""
    # Test Cursor rules output
    cursor_rules_file = str(tmp_path / ".cursorrules")
    ret_cursor = cli_main(["init-rules", "--type", "cursor", "--output", cursor_rules_file])
    assert ret_cursor == 0
    assert os.path.isfile(cursor_rules_file)
    with open(cursor_rules_file, "r", encoding="utf-8") as f:
        c_text = f.read()
    assert "Genesis Blackbox" in c_text
    assert "Zero Guessing" in c_text

    # Test Claude rules output
    claude_md_file = str(tmp_path / "CLAUDE.md")
    ret_claude = cli_main(["init-rules", "--type", "claude", "--output", claude_md_file])
    assert ret_claude == 0
    assert os.path.isfile(claude_md_file)
    with open(claude_md_file, "r", encoding="utf-8") as f:
        cl_text = f.read()
    assert "generate_reproduction_test" in cl_text

    # Test Windsurf rules output
    windsurf_file = str(tmp_path / ".windsurfrules")
    ret_windsurf = cli_main(["init-rules", "--type", "windsurf", "--output", windsurf_file])
    assert ret_windsurf == 0
    assert os.path.isfile(windsurf_file)


def test_mcp_generate_reproduction_test_tool(tmp_path):
    """Validates MCP tool generate_reproduction_test."""
    db_file = str(tmp_path / "wal.sqlite")
    ring = WalRingBuffer(db_path=db_file)
    hook = ExceptionHook(wal_ring=ring)

    try:
        helper_sample_crash_function(15, "mcp_test")
    except ValueError:
        exc_type, exc_val, tb = sys.exc_info()
        hook.handle_exception(exc_type, exc_val, tb)

    server = BlackboxMCPServer(db_path=db_file)

    # 1. Verify tool exists in tools/list
    list_resp = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = [t["name"] for t in list_resp["result"]["tools"]]
    assert "generate_reproduction_test" in tools

    # 2. Verify tool invocation via tools/call
    call_resp = server.handle_request({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "generate_reproduction_test",
            "arguments": {"db_path": db_file}
        }
    })
    assert "result" in call_resp
    text_code = call_resp["result"]["content"][0]["text"]
    assert "def test_reproduce_and_verify_fix():" in text_code
    ast.parse(text_code)
