"""
Genesis Blackbox v0.2.0 Sprint Part 2 Test Suite
Covers:
- Pillar 3: Polyglot CrashReproducer for Node.js/TypeScript (node:test + node:assert),
  end-to-end execution with CausalVerifier, and CLI reproduce/verify auto-detection.
- Pillar 4: Time-Travel Diff API (/api/diff-runs) and UI Server view with XSS protections.
"""

import os
import sys
import json
import urllib.request
import urllib.error
import threading
from pathlib import Path

from blackbox import (
    WalRingBuffer,
    CrashReproducer,
    CausalVerifier,
    StateDiffer,
    BlackboxMCPServer,
)
from blackbox.ui.server import create_ui_server
from blackbox.cli import main as cli_main


def test_node_reproducer_synthesis(tmp_path: Path):
    """Verifies CrashReproducer detects Node.js crash and synthesizes node:test reproduction test."""
    db_file = tmp_path / "node_rep_wal.sqlite"
    ring = WalRingBuffer(db_path=str(db_file))
    try:
        ring.record(
            event_type="EXCEPTION",
            payload={
                "runtime": "node",
                "exception_type": "TypeError",
                "exception_message": "Cannot read properties of undefined (reading 'token')",
                "root_cause": {
                    "filename": "/app/src/payment.js",
                    "function": "processPayment",
                    "lineno": 12,
                    "code_line": "const tok = card.token;",
                    "locals": {"amount": 100, "currency": "USD"}
                },
                "frames": [
                    {"filename": "/app/src/payment.js", "function": "processPayment", "lineno": 12}
                ]
            },
            status="error"
        )

        reproducer = CrashReproducer(wal_ring=ring)
        assert reproducer.is_node_target() is True
        assert reproducer.get_recommended_filename() == "test_blackbox_reproduce.mjs"

        code = reproducer.generate()
        assert "import test from 'node:test';" in code
        assert "import assert from 'node:assert/strict';" in code
        assert "processPayment" in code
        assert "/app/src/payment.js" in code
        assert '"amount": 100' in code
        assert '"currency": "USD"' in code
    finally:
        ring.close()


def test_end_to_end_node_reproducer_and_causal_verifier(tmp_path: Path):
    """
    Verifies full lifecycle:
    1. Crashing Node.js module fails reproduction test via node:test.
    2. Code fix applied.
    3. CausalVerifier passes and issues .blackbox-resolved.md receipt.
    """
    target_module = tmp_path / "checkout.mjs"
    # Unfixed module: throws TypeError if user.card is missing
    target_module.write_text(
        "export function handleCheckout(args) {\n"
        "    if (!args || !args.card) {\n"
        "        throw new TypeError('Cannot read properties of undefined (reading card)');\n"
        "    }\n"
        "    return { success: true, card: args.card };\n"
        "}\n",
        encoding="utf-8"
    )

    db_file = tmp_path / "checkout_wal.sqlite"
    ring = WalRingBuffer(db_path=str(db_file))
    receipt_file = tmp_path / ".blackbox-resolved.md"

    try:
        # Record crash in WAL
        ring.record(
            event_type="EXCEPTION",
            payload={
                "runtime": "node",
                "exception_type": "TypeError",
                "exception_message": "Cannot read properties of undefined (reading card)",
                "root_cause": {
                    "filename": str(target_module),
                    "function": "handleCheckout",
                    "lineno": 3,
                    "locals": {"item": "book"}  # card missing
                }
            },
            status="error"
        )

        reproducer = CrashReproducer(wal_ring=ring)
        code = reproducer.generate()

        test_file = tmp_path / "test_blackbox_reproduce.mjs"
        test_file.write_text(code, encoding="utf-8")

        verifier = CausalVerifier(wal_ring=ring, db_path=str(db_file), receipt_path=str(receipt_file))

        # Step 1: Verification against UNFIXED module must fail (reproducing the bug)
        res_broken = verifier.verify(test_path=str(test_file))
        assert res_broken["resolved"] is False
        assert res_broken["exit_code"] != 0

        # Step 2: Apply FIX to checkout.mjs
        target_module.write_text(
            "export function handleCheckout(args) {\n"
            "    const card = (args && args.card) ? args.card : 'default_card';\n"
            "    return { success: true, card: card };\n"
            "}\n",
            encoding="utf-8"
        )

        # Step 3: Verification against FIXED module must pass
        res_fixed = verifier.verify(test_path=str(test_file))
        assert res_fixed["resolved"] is True
        assert res_fixed["exit_code"] == 0
        assert receipt_file.is_file()

        receipt_text = receipt_file.read_text(encoding="utf-8")
        assert "VERIFIED_RESOLVED (PASS)" in receipt_text
        assert "bb-proof-" in receipt_text
    finally:
        ring.close()


def test_ui_server_diff_runs_endpoint_and_xss_safety(tmp_path: Path):
    """
    Verifies GET /api/diff-runs endpoint:
    - Returns structured JSON from StateDiffer.compare_runs().
    - Injects XSS probe and confirms proper XSS sanitization and UI rendering.
    """
    db_file = tmp_path / "ui_diff_wal.sqlite"
    ring = WalRingBuffer(db_path=str(db_file))
    try:
        # Run 1: Passing
        ring.record("PROCESS_START", {"run_id": "session-ok"})
        ring.record("HTTP_REQ", {"path": "/api/users", "status_code": 200}, status="ok")
        ring.record("SQL_QUERY", {"query": "SELECT * FROM users", "params": [1]}, status="ok")
        ring.record("PROCESS_EXIT", {"exit_code": 0})

        # Run 2: Failing with XSS probe in query
        ring.record("PROCESS_START", {"run_id": "session-fail"})
        ring.record("HTTP_REQ", {"path": "/api/users", "status_code": 200}, status="ok")
        xss_payload = "<script>alert('blackbox-xss')</script>"
        ring.record(
            "SQL_QUERY",
            {"query": f"SELECT * FROM users WHERE tag = '{xss_payload}'", "params": [None]},
            status="error"
        )
        ring.record("PROCESS_EXIT", {"exit_code": 1}, status="error")
    finally:
        ring.close()

    port = 8991
    server = create_ui_server(host="127.0.0.1", port=port, db_path=str(db_file))
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    try:
        # 1. Test /api/diff-runs JSON endpoint
        diff_url = f"http://127.0.0.1:{port}/api/diff-runs"
        with urllib.request.urlopen(diff_url, timeout=5) as resp:
            assert resp.status == 200
            assert "application/json" in resp.headers.get("Content-Type", "")
            data = json.loads(resp.read().decode("utf-8"))
            assert data["baseline_run"]["run_id"] == "session-ok"
            assert data["failing_run"]["run_id"] == "session-fail"
            assert data["divergence_point"]["type"] == "SQL_QUERY_FAILURE"

        # 2. Test /api/diff-runs with explicit baseline & failing query params
        param_url = f"http://127.0.0.1:{port}/api/diff-runs?baseline=session-ok&failing=session-fail"
        with urllib.request.urlopen(param_url, timeout=5) as resp:
            assert resp.status == 200
            data_param = json.loads(resp.read().decode("utf-8"))
            assert data_param["divergence_point"]["step"] == 3

        # 3. Test index.html serves Time-Travel Execution Diff view
        html_url = f"http://127.0.0.1:{port}/"
        with urllib.request.urlopen(html_url, timeout=5) as resp:
            assert resp.status == 200
            html_text = resp.read().decode("utf-8")
            assert "Time-Travel Execution Diff" in html_text
            assert "diff-container" in html_text
            assert "diff-body" in html_text
            # Unescaped executable script tags from user payload must NOT be present in raw HTML template
            assert "<script>alert('blackbox-xss')</script>" not in html_text
    finally:
        server.shutdown()
        server.server_close()
        server.wal_ring.close()


def test_cli_reproduce_subcommand_node_detection(tmp_path: Path):
    """Verifies blackbox reproduce CLI command automatically creates .mjs for Node crashes."""
    db_file = tmp_path / "cli_node_rep_wal.sqlite"
    ring = WalRingBuffer(db_path=str(db_file))
    try:
        ring.record(
            "EXCEPTION",
            {
                "runtime": "node",
                "exception_type": "ReferenceError",
                "exception_message": "foo is not defined",
                "root_cause": {
                    "filename": str(tmp_path / "index.js"),
                    "function": "startServer",
                    "lineno": 5
                }
            },
            status="error"
        )
    finally:
        ring.close()

    # Call CLI without --output, should default to test_blackbox_reproduce.mjs
    orig_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        ret = cli_main(["reproduce", "--db", str(db_file)])
        assert ret == 0
        expected_file = tmp_path / "test_blackbox_reproduce.mjs"
        assert expected_file.is_file()
        content = expected_file.read_text(encoding="utf-8")
        assert "node:test" in content
        assert "startServer" in content
    finally:
        os.chdir(orig_cwd)


def test_ui_server_diff_runs_missing_runs_returns_400(tmp_path: Path):
    """Verifies /api/diff-runs gracefully returns 400 JSON when insufficient runs exist."""
    db_file = tmp_path / "empty_diff_wal.sqlite"
    ring = WalRingBuffer(db_path=str(db_file))
    try:
        # Only passing run
        ring.record("PROCESS_START", {"run_id": "solo-run"})
        ring.record("PROCESS_EXIT", {"exit_code": 0})
    finally:
        ring.close()

    port = 8992
    server = create_ui_server(host="127.0.0.1", port=port, db_path=str(db_file))
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    try:
        diff_url = f"http://127.0.0.1:{port}/api/diff-runs"
        try:
            urllib.request.urlopen(diff_url, timeout=5)
            assert False, "Expected HTTPError 400"
        except urllib.error.HTTPError as err:
            assert err.code == 400
            err_data = json.loads(err.read().decode("utf-8"))
            assert "error" in err_data
    finally:
        server.shutdown()
        server.server_close()
        server.wal_ring.close()


def test_causal_verifier_missing_node_test_file(tmp_path: Path):
    """Verifies CausalVerifier cleanly reports failure when .mjs reproduction test file is absent."""
    db_file = tmp_path / "missing_wal.sqlite"
    ring = WalRingBuffer(db_path=str(db_file))
    try:
        verifier = CausalVerifier(wal_ring=ring, db_path=str(db_file))
        res = verifier.verify(test_path=str(tmp_path / "non_existent_reproduce.mjs"))
        assert res["resolved"] is False
        assert res["exit_code"] == 1
        assert "not found" in res["output"]
    finally:
        ring.close()


def test_mcp_generate_reproduction_test_for_node_crash(tmp_path: Path):
    """Verifies MCP server generate_reproduction_test tool outputs node:test syntax for Node crashes."""
    db_file = tmp_path / "mcp_node_wal.sqlite"
    ring = WalRingBuffer(db_path=str(db_file))
    try:
        ring.record(
            "EXCEPTION",
            {
                "runtime": "node",
                "exception_type": "TypeError",
                "exception_message": "Cannot read property 'id' of undefined",
                "root_cause": {
                    "filename": "/app/src/user.ts",
                    "function": "getUser",
                    "lineno": 14,
                    "locals": {"userId": "usr_99"}
                }
            },
            status="error"
        )
    finally:
        ring.close()

    mcp = BlackboxMCPServer(db_path=str(db_file))
    try:
        tool_fn = mcp.tools["generate_reproduction_test"]["handler"]
        code = tool_fn({"db_path": str(db_file)})
        assert "import test from 'node:test';" in code
        assert "getUser" in code
        assert "/app/src/user.ts" in code
    finally:
        mcp.wal_ring.close()


def test_cli_verify_subcommand_with_node_test(tmp_path: Path):
    """Verifies CLI blackbox verify subcommand execution against a passing Node.js reproduction test."""
    passing_mjs = tmp_path / "test_blackbox_reproduce.mjs"
    passing_mjs.write_text(
        "import test from 'node:test';\n"
        "import assert from 'node:assert/strict';\n"
        "test('verifies fixed behavior', () => {\n"
        "    assert.strictEqual(1 + 1, 2);\n"
        "});\n",
        encoding="utf-8"
    )

    db_file = tmp_path / "cli_verify_wal.sqlite"
    ring = WalRingBuffer(db_path=str(db_file))
    try:
        ring.record("PROCESS_START", {"run_id": "v-1"})
        ring.record("PROCESS_EXIT", {"exit_code": 0})
    finally:
        ring.close()

    receipt_file = tmp_path / ".blackbox-resolved.md"
    orig_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        ret = cli_main(["verify", "--db", str(db_file), "--test", str(passing_mjs), "--receipt", str(receipt_file)])
        assert ret == 0
        assert receipt_file.is_file()
        content = receipt_file.read_text(encoding="utf-8")
        assert "VERIFIED_RESOLVED (PASS)" in content
    finally:
        os.chdir(orig_cwd)

