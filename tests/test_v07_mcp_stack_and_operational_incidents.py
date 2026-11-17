"""
Unit and integration tests for Genesis Blackbox v0.7.0:
- GenesisStackManager.init_stack_config() & non-destructive merge across cursor/claude/windsurf
- CLI `blackbox init-stack`
- CausalCrashCompiler operational incident capsule & Recent MCP Tool Invocations table
- AutonomousHealPlanner operational incident detection
- BlackboxMCPServer `diagnose_mcp_ecosystem` (15 tools) & `mcp_ecosystem` in `get_full_incident_context`
"""

import json
import sys
from pathlib import Path

from blackbox import GenesisStackManager, __version__
from blackbox.cli import main as cli_main
from blackbox.compiler.autopsy import CausalCrashCompiler
from blackbox.compiler.heal_planner import AutonomousHealPlanner
from blackbox.mcp_server import BlackboxMCPServer
from blackbox.recorder.wal_ring import WalRingBuffer


def _seed_mcp_ecosystem_wal(db_path: Path) -> None:
    with WalRingBuffer(db_path=str(db_path)) as ring:
        ring.record(
            "MCP_TOOL",
            {"tool": "get_security_status", "session_id": "default"},
            source="pgsafe-mcp",
            duration_ms=1.5,
            status="ok",
        )
        ring.record(
            "SQL_QUERY",
            {
                "tool": "safe_query",
                "query": "DROP TABLE users;",
                "blocked": True,
                "error": "Security Guard Blocked Query: mutation statement DROP not allowed",
            },
            source="pgsafe-mcp",
            duration_ms=0.8,
            status="error",
        )
        ring.record(
            "HTTP_CLIENT",
            {
                "tool": "scrape_url",
                "method": "GET",
                "url": "http://127.0.0.1/admin",
                "error": "Blocked outbound IP '127.0.0.1': non-public address range.",
            },
            source="crawlengine-mcp",
            duration_ms=0.6,
            status="error",
        )
        ring.record(
            "SUBPROCESS",
            {
                "tool": "run_sandboxed_task",
                "command": "cat /etc/shadow",
                "returncode": 1,
                "error": "Command exited non-zero",
            },
            source="sysflow-mcp",
            duration_ms=4.2,
            status="error",
        )


def test_version_is_0_7_0():
    assert __version__ == "0.7.0"


def test_init_stack_non_destructive_merge_and_cli(tmp_path: Path):
    wal_path = tmp_path / ".blackbox" / "wal.sqlite"
    cursor_cfg = tmp_path / ".cursor" / "mcp.json"
    cursor_cfg.parent.mkdir(parents=True, exist_ok=True)
    cursor_cfg.write_text(
        json.dumps(
            {
                "theme": "dark",
                "mcpServers": {
                    "custom-user-mcp": {"command": "python", "args": ["custom.py"]}
                },
            }
        ),
        encoding="utf-8",
    )

    rc = cli_main([
        "init-stack",
        "--client", "cursor",
        "--output", str(cursor_cfg),
        "--db", str(wal_path),
    ])
    assert rc == 0

    merged = json.loads(cursor_cfg.read_text(encoding="utf-8"))
    assert merged["theme"] == "dark"
    assert "custom-user-mcp" in merged["mcpServers"]
    for srv in (
        "blackbox",
        "pgsafe-mcp",
        "sysflow-mcp",
        "crawlengine-mcp",
        "browserbridge-mcp",
        "omnibridge-mcp",
        "fastmcp-forge",
    ):
        assert srv in merged["mcpServers"]
        assert merged["mcpServers"][srv]["env"]["BLACKBOX_DB_PATH"] == str(wal_path.resolve())

    mgr = GenesisStackManager()
    claude_cfg = tmp_path / "claude_desktop_config.json"
    windsurf_cfg = tmp_path / "mcp_config.json"
    res_claude = mgr.init_stack_config(client="claude", output_path=str(claude_cfg), db_path=str(wal_path))
    res_windsurf = mgr.init_stack_config(client="windsurf", output_path=str(windsurf_cfg), db_path=str(wal_path))
    assert res_claude["client"] == "claude"
    assert res_windsurf["client"] == "windsurf"
    assert claude_cfg.is_file()
    assert windsurf_cfg.is_file()


def test_operational_incident_autopsy_and_mcp_tool_timeline(tmp_path: Path):
    wal_path = tmp_path / "wal.sqlite"
    _seed_mcp_ecosystem_wal(wal_path)

    with WalRingBuffer(db_path=str(wal_path)) as ring:
        compiler = CausalCrashCompiler(wal_ring=ring)
        op_capsule = compiler.compile()
        assert "# ⚠️ BLACKBOX OPERATIONAL INCIDENT CAPSULE" in op_capsule
        assert "pgsafe-mcp" in op_capsule or "sysflow-mcp" in op_capsule

        crash_capsule = compiler.compile(
            exception_data={
                "exception_type": "RuntimeError",
                "exception_message": "Downstream agent failed",
                "root_cause": {
                    "filename": "agent.py",
                    "lineno": 10,
                    "function": "run_step",
                    "locals": {"step": 2},
                },
                "frames": [],
            }
        )
        assert "### Recent MCP Tool Invocations" in crash_capsule
        assert "get_security_status" in crash_capsule
        assert "pgsafe-mcp" in crash_capsule


def test_heal_planner_detects_operational_incidents(tmp_path: Path):
    wal_path = tmp_path / "wal.sqlite"
    _seed_mcp_ecosystem_wal(wal_path)

    plan = AutonomousHealPlanner().build_heal_plan(
        wal_path=str(wal_path),
        baseline_path=str(tmp_path / "missing_baseline.sqlite"),
        root_dir=str(tmp_path),
    )
    assert plan["has_crash"] is False
    assert plan["has_operational_incident"] is True
    assert len(plan["operational_incidents"]) == 3
    assert "OperationalError[" in plan["root_cause"]["exception_type"]
    assert "Operational Incident:" in plan["markdown_plan"]


def test_mcp_diagnose_ecosystem_15_tools_and_full_context(tmp_path: Path):
    wal_path = tmp_path / "wal.sqlite"
    _seed_mcp_ecosystem_wal(wal_path)

    server = BlackboxMCPServer(db_path=str(wal_path))
    try:
        list_resp = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tool_names = [t["name"] for t in list_resp["result"]["tools"]]
        assert len(tool_names) == 15
        assert "diagnose_mcp_ecosystem" in tool_names

        eco_resp = server.handle_request({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "diagnose_mcp_ecosystem",
                "arguments": {"db_path": str(wal_path)},
            },
        })
        eco = json.loads(eco_resp["result"]["content"][0]["text"])
        assert eco["total_events_analyzed"] == 4
        assert eco["total_errors"] == 3
        assert eco["total_security_blocks"] == 2
        assert eco["active_sources_count"] == 3
        assert eco["servers"]["pgsafe-mcp"]["total_events"] == 2
        assert eco["servers"]["pgsafe-mcp"]["security_blocks"] == 1
        assert eco["servers"]["crawlengine-mcp"]["security_blocks"] == 1
        assert eco["servers"]["sysflow-mcp"]["error_events"] == 1

        ctx_resp = server.handle_request({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "get_full_incident_context",
                "arguments": {
                    "db_path": str(wal_path),
                    "baseline_db_path": str(tmp_path / "missing_baseline.sqlite"),
                    "root_dir": str(tmp_path),
                },
            },
        })
        ctx = json.loads(ctx_resp["result"]["content"][0]["text"])
        assert "mcp_ecosystem" in ctx
        assert ctx["mcp_ecosystem"]["total_errors"] == 3
        assert "# ⚠️ BLACKBOX OPERATIONAL INCIDENT CAPSULE" in ctx["autopsy_markdown"]
    finally:
        server.close()

