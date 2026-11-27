"""Comprehensive tests for Genesis Blackbox v1.0.0 release features."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import types
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from blackbox import (
    BlackboxMCPServer,
    CrashBundler,
    EgressMocker,
    GenesisStackManager,
    SqlInterceptor,
    SubprocessInterceptor,
    WalRingBuffer,
    __version__,
    auto_instrument,
    uninstall_exception_hook,
)
from blackbox.cli import main as cli_main
from blackbox.recorder.runner import run_target
from blackbox.ui.server import create_ui_server


class _LocalEchoHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        payload = json.dumps({"ok": True, "path": self.path, "token": "secret-body-val"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie", "sid=supersecretcookie")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _start_echo_server():
    srv = HTTPServer(("127.0.0.1", 0), _LocalEchoHandler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv, srv.server_address[1]


def test_version_and_zero_dep_packaging():
    assert __version__ == "1.0.0"
    repo_root = Path(__file__).resolve().parents[1]
    pyproject_text = (repo_root / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "1.0.0"' in pyproject_text
    assert "dependencies = []" in pyproject_text
    assert (repo_root / "Dockerfile").is_file()
    assert (repo_root / "smithery.yaml").is_file()
    glama = json.loads((repo_root / "glama.json").read_text(encoding="utf-8"))
    assert "johannschiffer" in glama["maintainers"]
    assert (repo_root.parent / "docker-compose.genesis-stack.yml").is_file()


def test_egress_mocker_urllib_and_optional_requests_httpx():
    srv, port = _start_echo_server()
    ring = WalRingBuffer(db_path=":memory:")
    orig_requests = sys.modules.get("requests")
    orig_requests_sess = sys.modules.get("requests.sessions")
    orig_httpx = sys.modules.get("httpx")
    try:
        # Build lightweight fake requests & httpx modules to test optional hooks deterministically
        fake_req_mod = types.ModuleType("requests")
        fake_sess_mod = types.ModuleType("requests.sessions")

        class _FakeResp:
            status_code = 201
            headers = {"X-Test": "1"}
            content = b'{"created":true,"password":"p"}'
            url = "https://api.example.local/items?api_key=abc123"

        class _FakeSession:
            headers = {"Authorization": "Bearer secret"}

            def request(self, method, url, *args, **kwargs):
                return _FakeResp()

        fake_sess_mod.Session = _FakeSession
        fake_req_mod.sessions = fake_sess_mod
        sys.modules["requests"] = fake_req_mod
        sys.modules["requests.sessions"] = fake_sess_mod

        fake_httpx = types.ModuleType("httpx")

        class _FakeRequest:
            def __init__(self, method, url):
                self.method = method
                self.url = url
                self.headers = {"x-api-key": "topsecret"}

        class _FakeHttpxResp:
            status_code = 200
            headers = {"content-type": "application/json"}
            content = b'{"ok":true}'

        class _FakeClient:
            def send(self, request, *args, **kwargs):
                return _FakeHttpxResp()

        class _FakeAsyncClient:
            async def send(self, request, *args, **kwargs):
                return _FakeHttpxResp()

        fake_httpx.Client = _FakeClient
        fake_httpx.AsyncClient = _FakeAsyncClient
        sys.modules["httpx"] = fake_httpx

        mocker = EgressMocker(wal_ring=ring).install()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/ping?api_key=mysecret", timeout=5) as r:
                assert r.status == 200
                data = json.loads(r.read().decode("utf-8"))
                assert data["ok"] is True

            _FakeSession().request("POST", "https://api.example.local/items?api_key=abc123")
            _FakeClient().send(_FakeRequest("GET", "https://httpx.local/health?token=xyz"))
        finally:
            mocker.uninstall()

        events = ring.query_recent(limit=10, event_type="HTTP_CLIENT")
        assert len(events) == 3
        urls = [e["payload"]["url"] for e in events]
        assert all("[REDACTED]" in u for u in urls)
        assert all("mysecret" not in u and "abc123" not in u and "xyz" not in u for u in urls)
    finally:
        if orig_requests is None:
            sys.modules.pop("requests", None)
        else:
            sys.modules["requests"] = orig_requests
        if orig_requests_sess is None:
            sys.modules.pop("requests.sessions", None)
        else:
            sys.modules["requests.sessions"] = orig_requests_sess
        if orig_httpx is None:
            sys.modules.pop("httpx", None)
        else:
            sys.modules["httpx"] = orig_httpx
        ring.close()
        srv.shutdown()
        srv.server_close()


def test_global_sqlite3_hook_skips_wal_and_records_app_sql(tmp_path):
    wal_path = str(tmp_path / ".blackbox" / "wal.sqlite")
    app_db = str(tmp_path / "app.sqlite")
    ring = WalRingBuffer(db_path=wal_path)
    SqlInterceptor.install_sqlite3_global(ring)
    try:
        conn = sqlite3.connect(app_db)
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);")
        conn.execute("INSERT INTO users (name) VALUES (?);", ("alice",))
        conn.executescript("INSERT INTO users (name) VALUES ('bob');")
        rows = conn.execute("SELECT name FROM users ORDER BY id;").fetchall()
        conn.close()
        assert [r[0] for r in rows] == ["alice", "bob"]
    finally:
        SqlInterceptor.uninstall_sqlite3_global()

    sql_events = ring.query_recent(limit=20, event_type="sql_query")
    ring.close()
    assert len(sql_events) >= 4
    assert all("blackbox_events" not in str(e["payload"].get("query", "")) for e in sql_events)


def test_run_target_and_auto_instrument_capture_egress_and_profile(tmp_path):
    srv, port = _start_echo_server()
    wal_path = str(tmp_path / ".blackbox" / "wal.sqlite")
    script = tmp_path / "target_app.py"
    script.write_text(
        f"import urllib.request\n"
        f"with urllib.request.urlopen('http://127.0.0.1:{port}/check?token=t1', timeout=5) as r:\n"
        f"    assert r.status == 200\n",
        encoding="utf-8",
    )
    try:
        rc = run_target(
            [str(script)],
            db_path=wal_path,
            auto_baseline=False,
            capture_egress=True,
            profile_resources=True,
        )
        assert rc == 0
        with WalRingBuffer(db_path=wal_path) as ring:
            http_ev = ring.query_recent(limit=10, event_type="HTTP_CLIENT")
            prof_ev = ring.query_recent(limit=10, event_type="RESOURCE_PROFILE")
            assert len(http_ev) == 1
            assert "[REDACTED]" in http_ev[0]["payload"]["url"]
            assert len(prof_ev) >= 1

        # Also test auto_instrument with capture_egress + profile_resources
        wal_path_2 = str(tmp_path / ".blackbox" / "wal2.sqlite")
        ring2 = auto_instrument(wal_path=wal_path_2, capture_egress=True, profile_resources=True)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/ai?api_key=k99", timeout=5) as r:
                assert r.status == 200
            assert len(ring2.query_recent(limit=5, event_type="HTTP_CLIENT")) == 1
            assert len(ring2.query_recent(limit=5, event_type="RESOURCE_PROFILE")) >= 1
        finally:
            EgressMocker.uninstall_global()
            SubprocessInterceptor.uninstall()
            uninstall_exception_hook()
            ring2.close()
    finally:
        srv.shutdown()
        srv.server_close()


def test_pytest_plugin_profile_and_no_egress_mock(tmp_path):
    wal_path = tmp_path / ".blackbox" / "wal.sqlite"
    sample_test = tmp_path / "test_sample.py"
    sample_test.write_text(
        "def test_ok():\n    data = [i for i in range(500)]\n    assert len(data) == 500\n",
        encoding="utf-8",
    )
    res = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(sample_test),
            "--blackbox",
            "--blackbox-db",
            str(wal_path),
            "--blackbox-profile",
            "--no-egress-mock",
            "--no-auto-baseline",
            "-q",
        ],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0, res.stderr + res.stdout
    with WalRingBuffer(db_path=str(wal_path)) as ring:
        prof = ring.query_recent(limit=10, event_type="RESOURCE_PROFILE")
        assert len(prof) >= 1


def test_stack_multiclient_doctor_and_cli(tmp_path, capsys):
    mgr = GenesisStackManager()
    wal_path = str(tmp_path / ".blackbox" / "wal.sqlite")
    with WalRingBuffer(db_path=wal_path) as ring:
        ring.record(
            event_type="MCP_TOOL",
            source="pgsafe-mcp",
            duration_ms=5.0,
            status="ok",
            payload={"tool": "list_tables"},
        )

    vs_path = str(tmp_path / ".vscode" / "mcp.json")
    res_vs = mgr.init_stack_config(client="vscode", output_path=vs_path, db_path=wal_path)
    assert res_vs["root_key"] == "servers"
    vs_data = json.loads(Path(vs_path).read_text(encoding="utf-8"))
    assert "servers" in vs_data and "blackbox" in vs_data["servers"]

    cc_path = str(tmp_path / ".mcp.json")
    res_cc = mgr.init_stack_config(client="claude-code", output_path=cc_path, db_path=wal_path)
    assert res_cc["root_key"] == "mcpServers"
    assert "pgsafe-mcp" in json.loads(Path(cc_path).read_text(encoding="utf-8"))["mcpServers"]

    ag_path = str(tmp_path / ".gemini" / "antigravity" / "mcp_config.json")
    res_ag = mgr.init_stack_config(client="antigravity", output_path=ag_path, db_path=wal_path)
    assert res_ag["client"] == "antigravity"
    assert os.path.isfile(ag_path)

    rc_doc = cli_main(["doctor", "--db", wal_path, "--root", str(tmp_path), "--json"])
    assert rc_doc == 0
    doc_json = json.loads(capsys.readouterr().out)
    assert doc_json["status"] == "healthy"
    assert doc_json["wal"]["event_count"] == 1

    rc_eco = cli_main(["ecosystem", "--db", wal_path, "--json"])
    assert rc_eco == 0
    eco_json = json.loads(capsys.readouterr().out)
    assert "pgsafe-mcp" in eco_json["servers"]


def test_mcp_resources_and_prompts(tmp_path):
    wal_path = str(tmp_path / ".blackbox" / "wal.sqlite")
    with WalRingBuffer(db_path=wal_path) as ring:
        ring.record(
            event_type="MCP_TOOL",
            source="sysflow-mcp",
            duration_ms=11.0,
            status="error",
            payload={"tool": "run_sandboxed_task", "error": "Command not in SYSFLOW_ALLOWED_COMMANDS"},
        )

    srv = BlackboxMCPServer(db_path=wal_path)
    try:
        init = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        caps = init["result"]["capabilities"]
        assert "resources" in caps and "prompts" in caps
        assert init["result"]["serverInfo"]["version"] == "1.0.0"

        rlist = srv.handle_request({"jsonrpc": "2.0", "id": 2, "method": "resources/list"})
        uris = {r["uri"] for r in rlist["result"]["resources"]}
        assert uris == {
            "blackbox://autopsy/latest",
            "blackbox://heal-plan/latest",
            "blackbox://ecosystem/health",
            "blackbox://timeline/recent",
        }

        for uri in uris:
            rread = srv.handle_request(
                {"jsonrpc": "2.0", "id": 3, "method": "resources/read", "params": {"uri": uri}}
            )
            assert len(rread["result"]["contents"]) == 1
            assert rread["result"]["contents"][0]["text"]

        bad_res = srv.handle_request(
            {"jsonrpc": "2.0", "id": 4, "method": "resources/read", "params": {"uri": "blackbox://missing"}}
        )
        assert bad_res["error"]["code"] == -32602

        plist = srv.handle_request({"jsonrpc": "2.0", "id": 5, "method": "prompts/list"})
        pnames = {p["name"] for p in plist["result"]["prompts"]}
        assert pnames == {"repair-latest-crash", "audit-mcp-stack"}

        pget = srv.handle_request(
            {"jsonrpc": "2.0", "id": 6, "method": "prompts/get", "params": {"name": "audit-mcp-stack"}}
        )
        assert "sysflow-mcp" in pget["result"]["messages"][0]["content"]["text"]
    finally:
        srv.close()


def test_web_ui_v2_endpoints_and_bundle_download(tmp_path):
    wal_path = str(tmp_path / ".blackbox" / "wal.sqlite")
    with WalRingBuffer(db_path=wal_path) as ring:
        ring.record(
            event_type="MCP_TOOL",
            source="pgsafe-mcp",
            duration_ms=4.0,
            status="error",
            trace_id="tr-ui-1",
            payload={"tool": "safe_query", "blocked": True, "error": "Security Guard Blocked"},
        )
        ring.record(
            event_type="HTTP_CLIENT",
            source="egress_mocker",
            duration_ms=9.0,
            status="ok",
            trace_id="tr-ui-2",
            payload={"method": "GET", "url": "https://example.local/ok", "status_code": 200},
        )

    server = create_ui_server(host="127.0.0.1", port=0, db_path=wal_path)
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"
    try:
        with urllib.request.urlopen(f"{base}/", timeout=5) as r:
            html = r.read().decode("utf-8")
            assert "Download Bundle (.zip)" in html
            assert "MCP Ecosystem" in html
            assert "Autonomous Heal Plan" in html
            assert "filter-type" in html

        with urllib.request.urlopen(f"{base}/api/timeline?source=pgsafe-mcp&status=error", timeout=5) as r:
            tl = json.loads(r.read().decode("utf-8"))
            assert tl["total_events"] == 1
            assert tl["events"][0]["source"] == "pgsafe-mcp"

        with urllib.request.urlopen(f"{base}/api/ecosystem", timeout=5) as r:
            eco = json.loads(r.read().decode("utf-8"))
            assert eco["total_security_blocks"] == 1

        with urllib.request.urlopen(f"{base}/api/perf", timeout=5) as r:
            perf = json.loads(r.read().decode("utf-8"))
            assert "markdown_report" in perf

        with urllib.request.urlopen(f"{base}/api/heal-plan", timeout=5) as r:
            heal = json.loads(r.read().decode("utf-8"))
            assert len(heal["steps"]) == 4

        with urllib.request.urlopen(f"{base}/api/bundle/download", timeout=5) as r:
            assert r.headers.get("Content-Type") == "application/zip"
            zip_bytes = r.read()

        dl_zip = tmp_path / "downloaded.zip"
        dl_zip.write_bytes(zip_bytes)
        with zipfile.ZipFile(dl_zip, "r") as zf:
            assert "system_manifest.json" in zf.namelist()
        inspected = CrashBundler().inspect_bundle(str(dl_zip))
        assert inspected["valid"] is True
    finally:
        server.shutdown()
        server.server_close()
