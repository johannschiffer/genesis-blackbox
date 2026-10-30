"""
Genesis Blackbox v0.2.0 Test Suite - Part 4
Covers:
- Pillar 7: Portable Crash Bundler (SQLite Online Backup, autopsy, diff, reproducer, SHA-256 system manifest, tamper detection)
- Pillar 8: Live SSE Streaming in Web UI (GET /api/live?once=1, text/event-stream, CORS, and security headers)
- Pillar 9: v0.2.0 Agent Rules Templates (cursorrules, claude_md, windsurf) and CLI/MCP integrations
"""

import os
import sys
import json
import zipfile
import threading
import urllib.request
import urllib.parse
from pathlib import Path

import pytest

from blackbox import (
    WalRingBuffer,
    CrashBundler,
    BlackboxMCPServer,
)
from blackbox.ui.server import create_ui_server
from blackbox.cli import main as cli_main


def test_crash_bundler_create_and_inspect_integrity(tmp_path: Path):
    """Verifies CrashBundler snapshots active WAL, packages incident artifacts, and verifies SHA-256 integrity."""
    wal_path = tmp_path / "wal.sqlite"
    ring = WalRingBuffer(db_path=str(wal_path))

    # Record baseline and failing runs
    r1 = "run_pass_1"
    ring.record("PROCESS_START", {"run_id": r1, "command": ["python", "app.py"]}, source="runner")
    ring.record("HTTP_REQ", {"method": "GET", "path": "/health"}, source="asgi")
    ring.record("EXIT", {"run_id": r1, "exit_code": 0}, source="runner")

    r2 = "run_fail_1"
    ring.record("PROCESS_START", {"run_id": r2, "command": ["python", "app.py"]}, source="runner")
    ring.record("HTTP_REQ", {"method": "POST", "path": "/data"}, source="asgi")
    ring.record("EXCEPTION", {
        "exception_type": "ValueError",
        "exception_message": "Invalid transaction state",
        "root_cause": {
            "function": "process_tx",
            "filename": str(tmp_path / "service.py"),
            "lineno": 10,
            "locals": {"tx_state": "corrupt"}
        },
        "frames": [
            {
                "function": "process_tx",
                "filename": str(tmp_path / "service.py"),
                "lineno": 10
            }
        ]
    }, source="exception_hook", status="error")
    ring.record("EXIT", {"run_id": r2, "exit_code": 1}, source="runner")
    ring.close()

    zip_out = tmp_path / "incident.zip"
    bundler = CrashBundler(db_path=str(wal_path), repo_root=str(tmp_path))
    res = bundler.create_bundle(output_zip=str(zip_out))

    assert res["success"] is True
    assert zip_out.is_file()
    assert res["size_bytes"] > 0
    assert "wal.sqlite" in res["files"]
    assert ".blackbox.md" in res["files"]
    assert ".blackbox-diff.md" in res["files"]
    assert "test_blackbox_reproduce.py" in res["files"]
    assert "system_manifest.json" in res["files"]

    # Verify inspection without extraction
    inspect_res = bundler.inspect_bundle(str(zip_out))
    assert inspect_res["valid"] is True
    assert len(inspect_res["corrupted_files"]) == 0

    manifest = inspect_res["manifest"]
    assert manifest is not None
    assert "created_at" in manifest
    assert "os" in manifest
    assert "python_version" in manifest
    assert "wal_stats" in manifest
    assert manifest["wal_stats"]["total_events"] >= 6
    assert "checksums" in manifest
    assert "wal.sqlite" in manifest["checksums"]


def test_crash_bundler_tamper_detection(tmp_path: Path):
    """Verifies CrashBundler detects modified or corrupted archive members via SHA-256 mismatch."""
    wal_path = tmp_path / "wal.sqlite"
    ring = WalRingBuffer(db_path=str(wal_path))
    ring.record("LOG", {"msg": "tamper test entry"}, source="test")
    ring.close()

    zip_original = tmp_path / "clean_bundle.zip"
    bundler = CrashBundler(db_path=str(wal_path), repo_root=str(tmp_path))
    bundler.create_bundle(output_zip=str(zip_original))

    # Read all original members
    members = {}
    with zipfile.ZipFile(str(zip_original), "r") as zf:
        for name in zf.namelist():
            members[name] = zf.read(name)

    # Tamper with .blackbox.md content while preserving the original system_manifest.json
    members[".blackbox.md"] = b"# TAMPERED CONTENT NOT MATCHING SHA256"

    tampered_zip = tmp_path / "tampered_bundle.zip"
    with zipfile.ZipFile(str(tampered_zip), "w", compression=zipfile.ZIP_DEFLATED) as zf_tampered:
        for name, data in members.items():
            zf_tampered.writestr(name, data)

    # Inspect tampered zip
    tampered_report = bundler.inspect_bundle(str(tampered_zip))
    assert tampered_report["valid"] is False
    assert len(tampered_report["corrupted_files"]) >= 1

    corr = tampered_report["corrupted_files"][0]
    assert corr["file"] == ".blackbox.md"
    assert "mismatch" in corr["error"].lower()


def test_cli_bundle_create_and_inspect(tmp_path: Path):
    """Verifies CLI blackbox bundle create and blackbox bundle inspect subcommands."""
    wal_path = tmp_path / "wal.sqlite"
    ring = WalRingBuffer(db_path=str(wal_path))
    ring.record("LOG", {"event": "cli_bundle_test"}, source="cli_test")
    ring.close()

    bundle_out = tmp_path / "cli_bundle.zip"

    # 1. blackbox bundle create
    ret_create = cli_main([
        "bundle", "create",
        "--output", str(bundle_out),
        "--db", str(wal_path)
    ])
    assert ret_create == 0
    assert bundle_out.exists()

    # 2. blackbox bundle inspect --json
    ret_inspect_json = cli_main([
        "bundle", "inspect",
        str(bundle_out),
        "--json"
    ])
    assert ret_inspect_json == 0

    # 3. blackbox bundle inspect (human format)
    ret_inspect_human = cli_main([
        "bundle", "inspect",
        str(bundle_out)
    ])
    assert ret_inspect_human == 0

    # 4. inspect invalid zip returns 1
    bad_zip = tmp_path / "invalid.zip"
    bad_zip.write_text("not a real zip content")
    ret_bad = cli_main(["bundle", "inspect", str(bad_zip)])
    assert ret_bad == 1


def test_mcp_crash_bundle_tools(tmp_path: Path):
    """Verifies Model Context Protocol tools for crash bundle creation and inspection."""
    wal_path = tmp_path / "wal.sqlite"
    ring = WalRingBuffer(db_path=str(wal_path))
    ring.record("LOG", {"event": "mcp_bundle_test"}, source="mcp")
    ring.close()

    server = BlackboxMCPServer(db_path=str(wal_path))
    assert "create_crash_bundle" in server.tools
    assert "blackbox_create_crash_bundle" in server.tools
    assert "inspect_crash_bundle" in server.tools
    assert "blackbox_inspect_crash_bundle" in server.tools

    bundle_zip = tmp_path / "mcp_bundle.zip"

    # Create bundle via MCP tool
    create_handler = server.tools["create_crash_bundle"]["handler"]
    create_res = create_handler({"output_zip": str(bundle_zip), "db_path": str(wal_path)})
    assert create_res["success"] is True
    assert bundle_zip.exists()

    # Inspect bundle via MCP tool
    inspect_handler = server.tools["inspect_crash_bundle"]["handler"]
    inspect_res = inspect_handler({"zip_path": str(bundle_zip)})
    assert inspect_res["valid"] is True
    assert inspect_res["manifest"] is not None


def test_ui_server_sse_live_endpoint(tmp_path: Path):
    """Verifies GET /api/live?once=1 returns valid Server-Sent Events headers and connected frame."""
    wal_path = tmp_path / "wal.sqlite"
    ring = WalRingBuffer(db_path=str(wal_path))
    ring.record("LOG", {"test": "sse_payload"}, source="sse_test")
    ring.close()

    server = create_ui_server(host="127.0.0.1", port=0, db_path=str(wal_path))
    port = server.server_address[1]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        url = f"http://127.0.0.1:{port}/api/live?once=1"
        req = urllib.request.Request(url, headers={"Origin": "http://127.0.0.1:8765"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            status = resp.status
            content_type = resp.headers.get("Content-Type", "")
            cache_control = resp.headers.get("Cache-Control", "")
            cors = resp.headers.get("Access-Control-Allow-Origin", "")
            body = resp.read().decode("utf-8")

        assert status == 200
        assert "text/event-stream" in content_type
        assert "no-cache" in cache_control
        assert cors in ("*", "http://127.0.0.1:8765")
        assert ": connected" in body
        assert "data:" in body
        assert '"type": "connected"' in body
    finally:
        server.shutdown()
        server.server_close()


def test_agent_rules_templates_v020_workflow(tmp_path: Path):
    """Verifies init-rules generates updated agent templates containing the v0.2.0 autonomous loop."""
    # 1. Cursor
    cursor_file = tmp_path / ".cursorrules"
    ret_cur = cli_main(["init-rules", "--type", "cursor", "--output", str(cursor_file)])
    assert ret_cur == 0
    cur_text = cursor_file.read_text()
    assert "compare_execution_runs" in cur_text
    assert "get_blast_radius" in cur_text
    assert "test_candidate_patch" in cur_text
    assert "verify_crash_resolution" in cur_text

    # 2. Claude
    claude_file = tmp_path / "CLAUDE.md"
    ret_claude = cli_main(["init-rules", "--type", "claude", "--output", str(claude_file)])
    assert ret_claude == 0
    claude_text = claude_file.read_text()
    assert "compare_execution_runs" in claude_text
    assert "get_repair_prompt" in claude_text
    assert "create_crash_bundle" in claude_text
    assert "inspect_crash_bundle" in claude_text

    # 3. Windsurf
    windsurf_file = tmp_path / ".windsurfrules"
    ret_ws = cli_main(["init-rules", "--type", "windsurf", "--output", str(windsurf_file)])
    assert ret_ws == 0
    ws_text = windsurf_file.read_text()
    assert "compare_execution_runs" in ws_text
    assert "get_repair_prompt" in ws_text
    assert "blackbox verify" in ws_text
