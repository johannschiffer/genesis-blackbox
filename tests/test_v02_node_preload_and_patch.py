"""
Genesis Blackbox v0.2.0 Test Suite - Part 3
Covers:
- Pillar 5: Node.js active runtime preload interceptor (fetch, http.request, uncaughtException, unhandledRejection, sensitive token/header redaction)
- Pillar 6: Polyglot PatchValidator (JS/TS syntax checking, .mjs reproduction test execution, guaranteed rollback) and PromptSynthesizer (AI repair prompt packet with code fences, autopsy, differ, blast radius)
- Subcommand: blackbox pack
- MCP Tool: get_repair_prompt
"""

import os
import sys
import json
import http.server
import threading
import subprocess
from pathlib import Path

import pytest

from blackbox import (
    WalRingBuffer,
    CodebaseGraph,
    PolyglotParser,
    PatchValidator,
    PromptSynthesizer,
    BlackboxMCPServer,
)
from blackbox.recorder.runner import run_target
from blackbox.cli import main as cli_main


class MockHttpHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "ok", "message": "hello from mock server"}')

    def log_message(self, format, *args):
        pass  # Quiet during tests


def _start_mock_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), MockHttpHandler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def test_node_preload_fetch_and_http_request_with_redaction(tmp_path: Path):
    """Verifies Node.js preload hook intercepts global fetch and http.request with header and query redaction."""
    server, port = _start_mock_server()
    wal_path = tmp_path / "wal.sqlite"

    node_script = tmp_path / "client.js"
    node_script.write_text(f"""
const http = require('http');

async function testFetch() {{
    try {{
        await fetch('http://127.0.0.1:{port}/api/users?token=supersecret123&api_key=privkey99&user=alice', {{
            headers: {{
                'Authorization': 'Bearer test-bearer-token',
                'Cookie': 'session=abc; secret_token=def',
                'X-Custom-Header': 'antislop-valid'
            }}
        }});
    }} catch (e) {{
        console.error('fetch error:', e);
    }}
}}

function testHttpRequest() {{
    return new Promise((resolve, reject) => {{
        const req = http.request({{
            hostname: '127.0.0.1',
            port: {port},
            path: '/api/v1/orders?secret=dontleakme&orderId=1001',
            method: 'GET',
            headers: {{
                'Authorization': 'Basic dXNlcjpwYXNz',
                'api_key': 'key_live_xyz',
                'X-Trace-Context': 'trace-123'
            }}
        }}, (res) => {{
            res.on('data', () => {{}});
            res.on('end', resolve);
        }});
        req.on('error', reject);
        req.end();
    }});
}}

async function main() {{
    await testFetch();
    await testHttpRequest();
}}
main();
""")

    ret = run_target(["node", str(node_script)], db_path=str(wal_path))
    assert ret == 0

    ring = WalRingBuffer(db_path=str(wal_path))
    events = ring.query_recent(limit=50, event_type="HTTP_CLIENT")
    ring.close()
    server.shutdown()

    assert len(events) >= 2

    # Check Fetch Event
    fetch_events = [e for e in events if e.get("source") == "node_fetch"]
    assert len(fetch_events) >= 1
    fetch_payload = fetch_events[0].get("payload", {})
    fetch_url = fetch_payload.get("url", "")
    assert "token=REDACTED" in fetch_url
    assert "api_key=REDACTED" in fetch_url
    assert "user=alice" in fetch_url

    fetch_headers = fetch_payload.get("headers", {})
    assert fetch_headers.get("Authorization") == "******** [REDACTED]"
    assert fetch_headers.get("Cookie") == "******** [REDACTED]"
    assert fetch_headers.get("X-Custom-Header") == "antislop-valid"

    # Check Http Event
    http_events = [e for e in events if e.get("source") == "node_http"]
    assert len(http_events) >= 1
    http_payload = http_events[0].get("payload", {})
    http_url = http_payload.get("url", "")
    assert "secret=REDACTED" in http_url
    assert "orderId=1001" in http_url

    http_headers = http_payload.get("headers", {})
    assert http_headers.get("Authorization") == "******** [REDACTED]"
    assert http_headers.get("api_key") == "******** [REDACTED]"
    assert http_headers.get("X-Trace-Context") == "trace-123"


def test_node_preload_uncaught_exception_and_unhandled_rejection(tmp_path: Path):
    """Verifies Node.js preload hook captures uncaughtException and unhandledRejection into wal.sqlite."""
    wal_path = tmp_path / "wal.sqlite"

    # 1. Uncaught Exception
    uncaught_script = tmp_path / "uncaught.js"
    uncaught_script.write_text("""
function crashFunction() {
    const account = null;
    account.profile.email;
}
crashFunction();
""")

    ret1 = run_target(["node", str(uncaught_script)], db_path=str(wal_path))
    assert ret1 != 0

    ring = WalRingBuffer(db_path=str(wal_path))
    uncaught_excs = ring.query_recent(limit=10, event_type="EXCEPTION")
    ring.close()

    assert len(uncaught_excs) >= 1
    p1 = uncaught_excs[0].get("payload", {})
    assert p1.get("runtime") == "node"
    assert p1.get("exception_type") == "TypeError"
    assert "reading 'profile'" in p1.get("exception_message", "")
    assert p1.get("context") == "uncaughtException"
    assert p1.get("root_cause", {}).get("function") == "crashFunction"
    assert p1.get("root_cause", {}).get("locals", {}).get("profile") is None
    assert "account.profile.email" in p1.get("root_cause", {}).get("code_line", "")

    # 2. Unhandled Rejection
    wal_path2 = tmp_path / "wal2.sqlite"
    rejection_script = tmp_path / "rejection.js"
    rejection_script.write_text("""
async function asyncFailingTask() {
    throw new Error('Async promise unhandled rejection marker');
}
asyncFailingTask();
""")

    ret2 = run_target(["node", str(rejection_script)], db_path=str(wal_path2))
    assert ret2 != 0

    ring2 = WalRingBuffer(db_path=str(wal_path2))
    rejection_excs = ring2.query_recent(limit=10, event_type="EXCEPTION")
    ring2.close()

    assert len(rejection_excs) >= 1
    p2 = rejection_excs[0].get("payload", {})
    assert p2.get("runtime") == "node"
    assert p2.get("context") == "unhandledRejection"
    assert "Async promise unhandled rejection marker" in p2.get("exception_message", "")


def test_patch_validator_javascript_clean_eval_and_rollback(tmp_path: Path):
    """Verifies PatchValidator validates JS patches against .mjs reproduction test and guarantees 100% rollback."""
    repo_dir = tmp_path / "test_repo"
    repo_dir.mkdir()

    # Initialize Git repository
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@genesis.local"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Genesis Test"], cwd=repo_dir, check=True)

    calc_file = repo_dir / "calc.js"
    original_calc_code = (
        "function divide(a, b) {\n"
        "    return a / b;\n"
        "}\n"
        "module.exports = { divide };\n"
    )
    calc_file.write_text(original_calc_code)

    repro_file = repo_dir / "test_blackbox_reproduce.mjs"
    repro_code = (
        "import test from 'node:test';\n"
        "import assert from 'node:assert/strict';\n"
        "import { createRequire } from 'node:module';\n"
        "const require = createRequire(import.meta.url);\n"
        "const { divide } = require('./calc.js');\n\n"
        "test('divide handles zero denominator by throwing', () => {\n"
        "    assert.throws(() => divide(10, 0), /Division by zero/);\n"
        "});\n"
    )
    repro_file.write_text(repro_code)

    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init repo"], cwd=repo_dir, check=True, capture_output=True)

    # Candidate fix patch
    valid_patch = """diff --git a/calc.js b/calc.js
--- a/calc.js
+++ b/calc.js
@@ -1,4 +1,5 @@
 function divide(a, b) {
+    if (b === 0) throw new Error("Division by zero");
     return a / b;
 }
 module.exports = { divide };
"""

    validator = PatchValidator(repo_root=str(repo_dir))
    result = validator.evaluate_patch(valid_patch, test_path="test_blackbox_reproduce.mjs")

    assert result["success"] is True
    assert result["syntax_valid"] is True
    assert result["reproducer_passed"] is True
    assert result["regressions_count"] == 0

    # Verify 100% Rollback: working tree is clean and file restored
    git_status = subprocess.run(["git", "status", "--porcelain"], cwd=repo_dir, capture_output=True, text=True)
    assert git_status.stdout.strip() == ""
    assert calc_file.read_text() == original_calc_code


def test_patch_validator_syntax_check_rejections(tmp_path: Path):
    """Verifies PatchValidator rejects invalid JS syntax (via node --check) and invalid TS syntax (via PolyglotParser)."""
    repo_dir = tmp_path / "syntax_repo"
    repo_dir.mkdir()

    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@genesis.local"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Genesis Test"], cwd=repo_dir, check=True)

    service_js = repo_dir / "service.js"
    service_js.write_text("function query() { return 1; }\nmodule.exports = { query };\n")

    helper_ts = repo_dir / "helper.ts"
    helper_ts.write_text("export function format(val: number): string { return String(val); }\n")

    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True, capture_output=True)

    validator = PatchValidator(repo_root=str(repo_dir))

    # 1. Broken JS syntax
    broken_js_patch = """diff --git a/service.js b/service.js
--- a/service.js
+++ b/service.js
@@ -1,2 +1,2 @@
-function query() { return 1; }
+function query( { return 1;
 module.exports = { query };
"""
    res_js = validator.evaluate_patch(broken_js_patch)
    assert res_js["success"] is False
    assert res_js["syntax_valid"] is False
    assert "Node syntax check failed" in res_js["output"]

    # Verify rollback
    st1 = subprocess.run(["git", "status", "--porcelain"], cwd=repo_dir, capture_output=True, text=True)
    assert st1.stdout.strip() == ""

    # 2. Broken TS syntax (unbalanced braces)
    broken_ts_patch = """diff --git a/helper.ts b/helper.ts
--- a/helper.ts
+++ b/helper.ts
@@ -1 +1,2 @@
-export function format(val: number): string { return String(val); }
+export function format(val: number): string {
+    return String(val);
"""
    res_ts = validator.evaluate_patch(broken_ts_patch)
    assert res_ts["success"] is False
    assert res_ts["syntax_valid"] is False
    assert "TypeScript/JSX syntax error" in res_ts["output"]

    # Verify rollback
    st2 = subprocess.run(["git", "status", "--porcelain"], cwd=repo_dir, capture_output=True, text=True)
    assert st2.stdout.strip() == ""


def test_prompt_synthesizer_polyglot_node_crash(tmp_path: Path):
    """Verifies PromptSynthesizer generates complete AI repair prompt packet with correct code fences and autopsy."""
    wal_path = tmp_path / "wal.sqlite"
    ring = WalRingBuffer(db_path=str(wal_path))

    # Create dummy TS source file
    src_file = tmp_path / "handler.ts"
    src_file.write_text("""
export function processTransaction(txId: string) {
    const gateway = null;
    return gateway.send(txId);
}
""")

    # Record baseline passing run
    r1 = "run_baseline_pass"
    ring.record("PROCESS_START", {"run_id": r1, "command": ["node", "app.js"]}, source="runner")
    ring.record("SQL_QUERY", {"query": "SELECT * FROM gateways", "params": []}, source="db")
    ring.record("EXIT", {"run_id": r1, "exit_code": 0}, source="runner")

    # Record failing run with uncaught V8 exception
    r2 = "run_failing_crash"
    ring.record("PROCESS_START", {"run_id": r2, "command": ["node", "app.js"]}, source="runner")
    ring.record("SQL_QUERY", {"query": "SELECT * FROM gateways WHERE active = 1", "params": []}, source="db")
    ring.record("EXCEPTION", {
        "runtime": "node",
        "exception_type": "TypeError",
        "exception_message": "Cannot read properties of null (reading 'send')",
        "context": "uncaughtException",
        "root_cause": {
            "function": "processTransaction",
            "filename": str(src_file),
            "lineno": 4,
            "colno": 12,
            "locals": {"gateway": None, "txId": "tx_9988"},
            "code_line": "return gateway.send(txId);"
        },
        "frames": [
            {
                "function": "processTransaction",
                "filename": str(src_file),
                "lineno": 4,
                "colno": 12
            }
        ],
        "raw_stderr": "TypeError: Cannot read properties of null (reading 'send')"
    }, source="node_uncaughtException", status="error")
    ring.record("EXIT", {"run_id": r2, "exit_code": 1}, source="runner")

    synth = PromptSynthesizer(wal_ring=ring, repo_root=str(tmp_path), db_path=str(wal_path))
    prompt = synth.synthesize()
    ring.close()

    assert "# 🛠️ BLACKBOX REPAIR PACKET: TypeError" in prompt
    assert "TypeScript" in prompt
    assert f"`{src_file}`" in prompt
    assert "processTransaction" in prompt
    # Code fence for reproduction test should be typescript/javascript
    assert "```typescript" in prompt or "```javascript" in prompt
    assert "test_blackbox_reproduce.mjs" in prompt
    # Flight recorder autopsy
    assert "BLACKBOX CRASH AUTOPSY CAPSULE" in prompt
    # Time-travel execution diff
    assert "Time-Travel Execution Diff" in prompt
    # Mandate
    assert "node --test test_blackbox_reproduce.mjs" in prompt
    assert "blackbox verify" in prompt


def test_cli_pack_and_mcp_get_repair_prompt(tmp_path: Path):
    """Verifies CLI blackbox pack and MCP get_repair_prompt tool."""
    wal_path = tmp_path / "wal.sqlite"
    ring = WalRingBuffer(db_path=str(wal_path))

    py_file = tmp_path / "service.py"
    py_file.write_text("def fetch_user(uid):\n    data = None\n    return data['name']\n")

    ring.record("EXCEPTION", {
        "exception_type": "TypeError",
        "exception_message": "'NoneType' object is not subscriptable",
        "root_cause": {
            "function": "fetch_user",
            "filename": str(py_file),
            "lineno": 3,
            "locals": {"data": None, "uid": 42},
            "code_line": "return data['name']"
        },
        "frames": [
            {
                "function": "fetch_user",
                "filename": str(py_file),
                "lineno": 3,
                "locals": {"data": None, "uid": 42}
            }
        ]
    }, source="exception_hook", status="error")
    ring.close()

    # 1. Test CLI pack
    out_packet = tmp_path / "packet.md"
    cli_ret = cli_main(["pack", "--db", str(wal_path), "--output", str(out_packet), "--root", str(tmp_path)])
    assert cli_ret == 0
    assert out_packet.exists()
    content = out_packet.read_text()
    assert "# 🛠️ BLACKBOX REPAIR PACKET: TypeError" in content
    assert "```python" in content
    assert "pytest test_blackbox_reproduce.py" in content

    # 2. Test MCP get_repair_prompt tool
    mcp_server = BlackboxMCPServer(db_path=str(wal_path))
    assert "get_repair_prompt" in mcp_server.tools
    assert "blackbox_get_repair_prompt" in mcp_server.tools

    prompt_res = mcp_server.tools["get_repair_prompt"]["handler"]({
        "db_path": str(wal_path),
        "repo_root": str(tmp_path)
    })
    assert "# 🛠️ BLACKBOX REPAIR PACKET: TypeError" in prompt_res
    assert "fetch_user" in prompt_res
    assert "pytest test_blackbox_reproduce.py" in prompt_res
