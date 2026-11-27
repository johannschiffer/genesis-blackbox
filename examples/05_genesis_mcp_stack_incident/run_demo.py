"""
Example 05: Genesis MCP Ecosystem Telemetry & Operational Incident Capsule
"""

import os
import sys
import tempfile

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from blackbox import (  # noqa: E402
    AutonomousHealPlanner,
    CausalCrashCompiler,
    GenesisStackManager,
    WalRingBuffer,
)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="bb_ex05_") as tmpdir:
        wal_path = os.path.join(tmpdir, ".blackbox", "wal.sqlite")
        with WalRingBuffer(db_path=wal_path) as ring:
            ring.record(
                event_type="MCP_TOOL",
                source="pgsafe-mcp",
                duration_ms=4.2,
                status="ok",
                payload={"tool": "list_tables", "rows": 6},
            )
            ring.record(
                event_type="MCP_TOOL",
                source="pgsafe-mcp",
                duration_ms=1.1,
                status="error",
                payload={"tool": "safe_query", "blocked": True, "error": "Security Guard Blocked: DROP TABLE denied"},
            )
            ring.record(
                event_type="MCP_TOOL",
                source="sysflow-mcp",
                duration_ms=18.5,
                status="error",
                payload={"tool": "run_sandboxed_task", "error": "Command not in SYSFLOW_ALLOWED_COMMANDS"},
            )
            ring.record(
                event_type="MCP_TOOL",
                source="crawlengine-mcp",
                duration_ms=65.0,
                status="ok",
                payload={"tool": "scrape_url", "url": "https://example.com"},
            )
            ring.record(
                event_type="MCP_TOOL",
                source="browserbridge-mcp",
                duration_ms=12.4,
                status="ok",
                payload={"tool": "extract_aom", "nodes": 48},
            )

            eco = GenesisStackManager().diagnose_ecosystem(db_path=wal_path)
            assert eco["active_sources_count"] == 4
            assert eco["total_errors"] == 2
            assert eco["total_security_blocks"] == 2

            autopsy = CausalCrashCompiler(wal_ring=ring).compile()
            assert "BLACKBOX OPERATIONAL INCIDENT CAPSULE" in autopsy
            assert "pgsafe-mcp" in autopsy and "sysflow-mcp" in autopsy

            heal_plan = AutonomousHealPlanner().build_heal_plan(wal_path=wal_path, root_dir=REPO_ROOT)
            assert heal_plan["has_operational_incident"] is True
            assert len(heal_plan["operational_incidents"]) == 2

            print("[Example 05] PASS: Unified 4-server MCP telemetry, operational incident capsule, and ecosystem diagnostics verified.")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
