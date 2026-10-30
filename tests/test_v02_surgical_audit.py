"""
Regression tests for the v0.2.0 surgical audit: run ordering, Node preload redaction,
polyglot regex literals, differ boundaries, bundle hardening, patch sandboxing,
reproducer escaping, prompt blast radius, MCP tool surface, and UI server safety.
"""

import ast
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import zipfile
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from blackbox.recorder.wal_ring import WalRingBuffer
from blackbox.recorder.runner import run_target
from blackbox.recorder.bundle import CrashBundler
from blackbox.ast_engine.polyglot import PolyglotParser, parse_v8_stack_trace
from blackbox.ast_engine.graph import CodebaseGraph
from blackbox.compiler.state_differ import StateDiffer
from blackbox.compiler.patch_validator import PatchValidator
from blackbox.compiler.reproducer import CrashReproducer, _safe_repr
from blackbox.compiler.prompt_synth import PromptSynthesizer
from blackbox.mcp_server import BlackboxMCPServer
from blackbox.ui.server import create_ui_server
from blackbox.cli import main as cli_main


NODE_BIN = shutil.which("node")
requires_node = pytest.mark.skipif(NODE_BIN is None, reason="node not installed")


class _OkHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _start_ok_server():
    srv = HTTPServer(("127.0.0.1", 0), _OkHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def _git_repo(path: Path):
    subprocess.run(["git", "init"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=str(path), check=True)


def _commit_all(path: Path):
    subprocess.run(["git", "add", "."], cwd=str(path), check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(path), check=True, capture_output=True)


# Runner and Node preload

@requires_node
def test_node_events_land_before_subprocess_exit(tmp_path: Path):
    srv, port = _start_ok_server()
    script = tmp_path / "client.js"
    script.write_text(
        "const http = require('http');\n"
        f"http.get('http://user:hunter2@127.0.0.1:{port}/ping?token=abc', (res) => {{\n"
        "  res.resume();\n"
        "  res.on('end', () => { throw new Error('boom after http'); });\n"
        "});\n",
        encoding="utf-8",
    )
    db = tmp_path / "wal.sqlite"
    try:
        rc = run_target([NODE_BIN, str(script)], db_path=str(db))
    finally:
        srv.shutdown()
    assert rc != 0

    with WalRingBuffer(db_path=str(db)) as ring:
        timeline = ring.get_timeline(limit=100)
    types = [e["event_type"] for e in timeline]
    assert "SUBPROCESS_EXIT" in types
    exit_idx = types.index("SUBPROCESS_EXIT")
    http_idx = types.index("HTTP_CLIENT")
    exc_idx = types.index("EXCEPTION")
    assert http_idx < exit_idx and exc_idx < exit_idx

    http_payload = timeline[http_idx]["payload"]
    assert "hunter2" not in http_payload["url"]
    assert f":{port}/" in http_payload["url"]
    assert "token=REDACTED" in http_payload["url"]


@requires_node
def test_node_fetch_tuple_headers_are_redacted(tmp_path: Path):
    srv, port = _start_ok_server()
    script = tmp_path / "fetcher.js"
    script.write_text(
        f"fetch('http://127.0.0.1:{port}/x', {{ headers: [['Authorization', 'Bearer leak-me'], ['X-Ok', 'yes']] }})\n"
        "  .then(r => r.text());\n",
        encoding="utf-8",
    )
    db = tmp_path / "wal.sqlite"
    try:
        rc = run_target([NODE_BIN, str(script)], db_path=str(db))
    finally:
        srv.shutdown()
    assert rc == 0
    with WalRingBuffer(db_path=str(db)) as ring:
        evs = ring.query_recent(limit=10, event_type="HTTP_CLIENT")
    headers = evs[0]["payload"]["headers"]
    assert headers["Authorization"] == "******** [REDACTED]"
    assert headers["X-Ok"] == "yes"
    assert "leak-me" not in json.dumps(evs)


@requires_node
def test_node_circular_non_error_throw_is_recorded(tmp_path: Path):
    script = tmp_path / "circular.js"
    script.write_text("const a = { kind: 'cfg' }; a.self = a; throw a;\n", encoding="utf-8")
    db = tmp_path / "wal.sqlite"
    rc = run_target([NODE_BIN, str(script)], db_path=str(db))
    assert rc != 0
    with WalRingBuffer(db_path=str(db)) as ring:
        excs = ring.query_recent(limit=5, event_type="EXCEPTION")
    assert len(excs) >= 1


@requires_node
def test_node_esm_stack_frames_resolve_file_urls(tmp_path: Path):
    script = tmp_path / "crash.mjs"
    script.write_text(
        "export function explode() {\n"
        "  const obj = undefined;\n"
        "  return obj.field;\n"
        "}\n"
        "explode();\n",
        encoding="utf-8",
    )
    db = tmp_path / "wal.sqlite"
    rc = run_target([NODE_BIN, str(script)], db_path=str(db))
    assert rc != 0
    with WalRingBuffer(db_path=str(db)) as ring:
        excs = ring.query_recent(limit=5, event_type="EXCEPTION")
    root = excs[0]["payload"]["root_cause"]
    assert not str(root["filename"]).startswith("file://")
    assert os.path.basename(root["filename"]) == "crash.mjs"
    assert "obj.field" in root.get("code_line", "")


# Polyglot parser and graph

def test_polyglot_regex_literals_do_not_break_syntax_or_callees():
    code = (
        "export function clean(s) {\n"
        "  const q = s.replace(/['\"({[]/g, '');\n"
        "  const isUrl = /^https?:\\/\\//.test(q);\n"
        "  const ratio = total / count / 2;\n"
        "  // fakeCallInComment()\n"
        "  const msg = 'fakeCallInString()';\n"
        "  return normalize(q, isUrl, ratio, msg);\n"
        "}\n"
    )
    parser = PolyglotParser(file_path="clean.ts")
    parser.validate_syntax(code)
    res = parser.parse(code)
    json.dumps(res)
    calls = res["functions"]["clean"]["calls"]
    assert "normalize" in calls
    assert "fakeCallInComment" not in calls
    assert "fakeCallInString" not in calls


def test_polyglot_parse_is_idempotent_after_validate():
    code = "import { a } from './a';\nexport function f() { return a(); }\n"
    parser = PolyglotParser(file_path="f.js")
    parser.validate_syntax(code)
    first = parser.parse(code)
    second = parser.parse(code)
    assert first["imports"] == second["imports"]
    assert len(second["imports"]) == len(set(json.dumps(i, sort_keys=True) for i in second["imports"]))


def test_v8_stack_trace_decodes_file_urls(tmp_path: Path):
    src = tmp_path / "my app.mjs"
    src.write_text("line1\nthrow new Error('x');\n", encoding="utf-8")
    url = src.as_uri()
    stderr = f"Error: x\n    at run ({url}:2:7)\n"
    parsed = parse_v8_stack_trace(stderr)
    assert parsed["root_cause"]["filename"] == str(src)
    assert parsed["root_cause"]["code_line"] == "throw new Error('x');"


def test_codebase_graph_evicts_stale_symbols_on_reparse():
    graph = CodebaseGraph()
    graph.parse_source("def old_fn():\n    return helper()\n", filename="/repo/mod.py")
    assert "old_fn" in graph.nodes
    graph.parse_source("def new_fn():\n    return 1\n", filename="/repo/mod.py")
    assert "old_fn" not in graph.nodes
    assert "old_fn" not in graph.callers_map.get("helper", set())


# State differ

def _two_runs(ring, b_sql, f_sql, exit_type="EXIT"):
    ring.record("PROCESS_START", {"run_id": "r1"}, source="runner")
    ring.record("SQL_QUERY", b_sql, source="db")
    ring.record(exit_type, {"run_id": "r1", "exit_code": 0}, source="runner")
    ring.record("PROCESS_START", {"run_id": "r2"}, source="runner")
    ring.record("SQL_QUERY", f_sql, source="db")
    ring.record(exit_type, {"run_id": "r2", "exit_code": 1}, source="runner", status="error")


def test_state_differ_detects_query_text_change_and_exit_boundary(tmp_path: Path):
    with WalRingBuffer(db_path=str(tmp_path / "w.sqlite")) as ring:
        _two_runs(ring, {"query": "SELECT * FROM t", "params": []},
                  {"query": "SELECT * FROM t WHERE a = 1", "params": []})
        differ = StateDiffer(wal_ring=ring)
        runs = differ.extract_runs()
        assert len(runs) == 2
        res = differ.diff()
        assert res["sql_diff"][0]["divergence"] == "SQL query structure changed"


def test_state_differ_reports_added_named_param(tmp_path: Path):
    with WalRingBuffer(db_path=str(tmp_path / "w.sqlite")) as ring:
        differ = StateDiffer(wal_ring=ring)
        msg = differ._diff_param_shapes({"a": 1}, {"a": 1, "b": 2})
        assert msg == "Named param 'b' added in crashing run"


def test_state_differ_markdown_escapes_pipes(tmp_path: Path):
    with WalRingBuffer(db_path=str(tmp_path / "w.sqlite")) as ring:
        _two_runs(ring, {"query": "SELECT a || b FROM t", "params": []},
                  {"query": "SELECT a || c FROM t", "params": []})
        differ = StateDiffer(wal_ring=ring)
        md = differ.compile_markdown(differ.diff())
        assert "a \\|\\| b" in md


# Bundle inspection

def _write_bundle(path: Path, members, manifest):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members:
            zf.writestr(name, data)
        zf.writestr("system_manifest.json", json.dumps(manifest))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_bundle_rejects_unverified_extra_member(tmp_path: Path):
    data = b"hello"
    zp = tmp_path / "b.zip"
    _write_bundle(zp, [("autopsy.md", data), ("evil.sh", b"rm -rf /")],
                  {"files": ["autopsy.md"], "checksums": {"autopsy.md": _sha(data)}})
    res = CrashBundler().inspect_bundle(str(zp))
    assert res["valid"] is False


def test_bundle_rejects_traversal_member(tmp_path: Path):
    data = b"x"
    zp = tmp_path / "b.zip"
    _write_bundle(zp, [("../evil", data)],
                  {"files": ["../evil"], "checksums": {"../evil": _sha(data)}})
    res = CrashBundler().inspect_bundle(str(zp))
    assert res["valid"] is False


def test_bundle_rejects_empty_checksums(tmp_path: Path):
    zp = tmp_path / "b.zip"
    _write_bundle(zp, [], {"files": [], "checksums": {}})
    res = CrashBundler().inspect_bundle(str(zp))
    assert res["valid"] is False


def test_bundle_rejects_duplicate_entries(tmp_path: Path):
    data = b"a"
    zp = tmp_path / "b.zip"
    with pytest.warns(UserWarning):
        _write_bundle(zp, [("autopsy.md", data), ("autopsy.md", b"b")],
                      {"files": ["autopsy.md"], "checksums": {"autopsy.md": _sha(data)}})
    res = CrashBundler().inspect_bundle(str(zp))
    assert res["valid"] is False


def test_bundle_round_trip_snapshot_is_valid(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "wal.sqlite"
    with WalRingBuffer(db_path=str(db)) as ring:
        ring.record("LOG", {"msg": "hi"})
    out = tmp_path / "bundle.zip"
    CrashBundler(db_path=str(db), repo_root=str(tmp_path)).create_bundle(output_zip=str(out), db_path=str(db))
    res = CrashBundler().inspect_bundle(str(out))
    assert res["valid"] is True, res


# Patch validator

def _repo_with_file(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)
    (repo / "app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _commit_all(repo)
    return repo


def test_patch_validator_rejects_custom_prefix_traversal(tmp_path: Path):
    repo = _repo_with_file(tmp_path)
    patch_str = (
        "--- x/../../outside.py\n"
        "+++ y/../../outside.py\n"
        "@@ -0,0 +1 @@\n"
        "+print('pwned')\n"
    )
    v = PatchValidator(repo_root=str(repo))
    assert v.validate_patch_dry_run(patch_str) is False
    res = v.evaluate_patch(patch_str)
    assert res["success"] is False
    assert "Security violation" in res["output"]
    assert not (tmp_path / "outside.py").exists()


def test_patch_validator_rejects_git_dir_target(tmp_path: Path):
    repo = _repo_with_file(tmp_path)
    v = PatchValidator(repo_root=str(repo))
    assert v.is_path_safe(str(repo / ".git" / "config")) is False
    assert v.is_path_safe(str(repo / "app.py")) is True


def test_patch_validator_rejects_symlink_escape(tmp_path: Path):
    repo = _repo_with_file(tmp_path)
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    os.symlink(str(outside), str(repo / "link"))
    v = PatchValidator(repo_root=str(repo))
    assert v.is_path_safe(str(repo / "link" / "victim.py")) is False


def test_patch_validator_rejects_symlink_mode(tmp_path: Path):
    repo = _repo_with_file(tmp_path)
    patch_str = (
        "diff --git a/evil b/evil\n"
        "new file mode 120000\n"
        "--- /dev/null\n"
        "+++ b/evil\n"
        "@@ -0,0 +1 @@\n"
        "+/etc/passwd\n"
        "\\ No newline at end of file\n"
    )
    res = PatchValidator(repo_root=str(repo)).evaluate_patch(patch_str)
    assert res["success"] is False
    assert "symlink" in res["output"]
    assert not os.path.lexists(repo / "evil")


def test_patch_validator_rejects_out_of_repo_test_path(tmp_path: Path):
    repo = _repo_with_file(tmp_path)
    (tmp_path / "evil_test.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    patch_str = (
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def f():\n"
        "-    return 1\n"
        "+    return 2\n"
    )
    res = PatchValidator(repo_root=str(repo)).evaluate_patch(patch_str, test_path="../evil_test.py")
    assert res["success"] is False
    assert "test path" in res["output"]
    assert "return 1" in (repo / "app.py").read_text(encoding="utf-8")


def test_patch_validator_restores_when_reverse_apply_times_out(tmp_path: Path):
    repo = _repo_with_file(tmp_path)
    patch_str = (
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def f():\n"
        "-    return 1\n"
        "+    return 2\n"
    )
    v = PatchValidator(repo_root=str(repo))
    real_run = subprocess.run

    def flaky_run(cmd, *args, **kwargs):
        if cmd[:3] == ["git", "apply", "--reverse"]:
            raise subprocess.TimeoutExpired(cmd, 10)
        return real_run(cmd, *args, **kwargs)

    with patch("blackbox.compiler.patch_validator.subprocess.run", side_effect=flaky_run):
        v.evaluate_patch(patch_str)
    assert (repo / "app.py").read_text(encoding="utf-8") == "def f():\n    return 1\n"


# Reproducer

def test_safe_repr_handles_nan_and_inf():
    assert _safe_repr(float("nan")) == "float('nan')"
    assert _safe_repr(float("inf")) == "float('inf')"
    assert _safe_repr(float("-inf")) == "float('-inf')"
    ast.parse(_safe_repr({"x": float("nan")}))


def test_python_reproducer_escapes_hostile_names(tmp_path: Path):
    with WalRingBuffer(db_path=str(tmp_path / "w.sqlite")) as ring:
        ring.record("EXCEPTION", {
            "exception_type": 'Bad"""Type\nimport os',
            "exception_message": "m",
            "frames": [],
            "root_cause": {
                "filename": str(tmp_path / "x.py"),
                "lineno": 3,
                "function": 'fn"""\nos.system("id")',
                "locals": {'k"]: 1}; import os; x = {"': 1, "ratio": float("inf")},
            },
        }, status="error")
        code = CrashReproducer(ring).generate()
    tree = ast.parse(code)
    os_imports = [a for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names if a.name == "os"]
    assert len(os_imports) == 1
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "system"]
    assert calls == []
    assert "float('inf')" in code


@requires_node
def test_node_reproducer_escapes_jsdoc_and_resolves_qualified_names(tmp_path: Path):
    target = tmp_path / "svc.mjs"
    target.write_text("export function processPayment(args) { return 'ok'; }\n", encoding="utf-8")
    with WalRingBuffer(db_path=str(tmp_path / "w.sqlite")) as ring:
        ring.record("EXCEPTION", {
            "runtime": "node",
            "exception_type": "Type*/Error",
            "exception_message": "bad",
            "frames": [],
            "root_cause": {
                "filename": str(target),
                "lineno": 1,
                "function": "Object.processPayment",
                "locals": {},
            },
        }, status="error")
        code = CrashReproducer(ring).generate()
    header = code.split("*/", 1)[0]
    assert "Type*/Error" not in header
    test_file = tmp_path / "test_repro.mjs"
    test_file.write_text(code, encoding="utf-8")
    res = subprocess.run([NODE_BIN, "--test", str(test_file)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stdout + res.stderr


# Prompt synthesizer

def test_prompt_synth_prunes_node_modules_and_finds_same_file_callers(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    culprit = src / "pay.ts"
    culprit.write_text(
        "export function charge(x) { return x.amount; }\n"
        "export function checkout(o) { return charge(o); }\n",
        encoding="utf-8",
    )
    nm = tmp_path / "node_modules" / "dep"
    nm.mkdir(parents=True)
    (nm / "index.js").write_text("function vendorCaller() { return charge(1); }\n", encoding="utf-8")
    (src / "api.js").write_text("function route(req) { return charge(req); }\n", encoding="utf-8")

    with WalRingBuffer(db_path=str(tmp_path / "w.sqlite")) as ring:
        synth = PromptSynthesizer(wal_ring=ring, repo_root=str(tmp_path))
        info = synth._resolve_blast_radius(str(culprit), "Object.charge", True)
    assert "checkout()" in info
    assert "route()" in info
    assert "vendorCaller" not in info


def test_prompt_synth_finds_cross_file_python_callers(tmp_path: Path):
    (tmp_path / "core.py").write_text("def compute(x):\n    return 1 / x\n", encoding="utf-8")
    (tmp_path / "service.py").write_text(
        "from core import compute\n\ndef handler(v):\n    return compute(v)\n", encoding="utf-8"
    )
    with WalRingBuffer(db_path=str(tmp_path / "w.sqlite")) as ring:
        synth = PromptSynthesizer(wal_ring=ring, repo_root=str(tmp_path))
        info = synth._resolve_blast_radius(str(tmp_path / "core.py"), "compute", False)
    assert "handler" in info


def test_prompt_synth_close_only_closes_owned_ring(tmp_path: Path):
    db = str(tmp_path / "w.sqlite")
    shared = WalRingBuffer(db_path=db)
    PromptSynthesizer(wal_ring=shared, repo_root=str(tmp_path)).close()
    assert shared.query_recent(limit=1) == []
    shared.close()


# MCP server and CLI

def test_mcp_tools_list_exposes_all_tools(tmp_path: Path):
    server = BlackboxMCPServer(db_path=str(tmp_path / "w.sqlite"))
    try:
        resp = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {t["name"] for t in resp["result"]["tools"]}
        assert names == {
            "get_crash_autopsy", "generate_reproduction_test", "verify_crash_resolution",
            "test_candidate_patch", "get_blast_radius", "query_execution_timeline",
            "record_event", "compare_execution_runs", "get_repair_prompt",
            "create_crash_bundle", "inspect_crash_bundle",
        }
    finally:
        server.close()


@requires_node
def test_mcp_candidate_patch_autodetects_mjs_reproducer(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)
    (repo / "lib.mjs").write_text("export function f(a) { return a.b.c; }\n", encoding="utf-8")
    (repo / "test_blackbox_reproduce.mjs").write_text(
        "import test from 'node:test';\n"
        "import assert from 'node:assert/strict';\n"
        "import { f } from './lib.mjs';\n"
        "test('no crash', () => { assert.equal(f({}), undefined); });\n",
        encoding="utf-8",
    )
    _commit_all(repo)
    monkeypatch.chdir(repo)
    patch_str = (
        "--- a/lib.mjs\n"
        "+++ b/lib.mjs\n"
        "@@ -1 +1 @@\n"
        "-export function f(a) { return a.b.c; }\n"
        "+export function f(a) { return a.b?.c; }\n"
    )
    server = BlackboxMCPServer(db_path=str(tmp_path / "w.sqlite"))
    try:
        resp = server.handle_request({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "test_candidate_patch", "arguments": {"patch_diff": patch_str}},
        })
        verdict = json.loads(resp["result"]["content"][0]["text"])
        assert verdict["success"] is True, verdict
        assert verdict["reproducer_passed"] is True
    finally:
        server.close()
    assert "a.b.c;" in (repo / "lib.mjs").read_text(encoding="utf-8")


def test_cli_reproduce_defaults_to_blackbox_python_name(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = str(tmp_path / "w.sqlite")
    with WalRingBuffer(db_path=db) as ring:
        ring.record("EXCEPTION", {
            "exception_type": "ValueError", "exception_message": "m", "frames": [],
            "root_cause": {"filename": str(tmp_path / "a.py"), "lineno": 1, "function": "f", "locals": {}},
        }, status="error")
    assert cli_main(["reproduce", "--db", db]) == 0
    assert (tmp_path / "test_blackbox_reproduce.py").is_file()


# UI server

@pytest.fixture
def ui_server(tmp_path: Path):
    db = str(tmp_path / "ui.sqlite")
    with WalRingBuffer(db_path=db) as ring:
        ring.record("LOG", {"msg": "seed"})
    srv = create_ui_server(host="127.0.0.1", port=0, db_path=db)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _raw_get(port: int, path: str, host_header: str, timeout: float = 5.0):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    conn.putrequest("GET", path, skip_host=True)
    conn.putheader("Host", host_header)
    conn.endheaders()
    return conn, conn.getresponse()


def test_ui_rejects_dns_rebinding_host(ui_server):
    port = ui_server.server_address[1]
    conn, resp = _raw_get(port, "/api/timeline", "evil.attacker.com")
    assert resp.status == 403
    conn.close()
    conn, resp = _raw_get(port, "/api/status", f"localhost:{port}")
    assert resp.status == 200
    conn.close()


def test_ui_live_has_no_wildcard_cors(ui_server):
    port = ui_server.server_address[1]
    conn, resp = _raw_get(port, "/api/live?once=1", f"127.0.0.1:{port}")
    assert resp.status == 200
    assert resp.getheader("Access-Control-Allow-Origin") is None
    resp.read()
    conn.close()


def test_ui_server_close_ends_live_streams_promptly(tmp_path: Path):
    db = str(tmp_path / "ui.sqlite")
    srv = create_ui_server(host="127.0.0.1", port=0, db_path=db)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    conn, resp = _raw_get(port, "/api/live", f"127.0.0.1:{port}", timeout=5.0)
    assert resp.status == 200
    resp.fp.readline()

    srv.shutdown()
    start = time.monotonic()
    srv.server_close()
    assert srv._stop_event.is_set()
    # The stream thread wakes from its wait and ends the response.
    deadline = time.monotonic() + 3.0
    ended = False
    while time.monotonic() < deadline:
        try:
            chunk = resp.fp.readline()
        except (socket.timeout, OSError):
            break
        if chunk == b"":
            ended = True
            break
    conn.close()
    assert ended
    assert time.monotonic() - start < 3.0
