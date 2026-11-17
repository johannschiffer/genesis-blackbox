"""
Genesis Blackbox: Unified MCP Stack Provisioner & Ecosystem Diagnostics
Configures multi-server MCP environments sharing a single SQLite WAL flight recorder
and aggregates operational health across connected Genesis MCP servers.
"""

import json
import os
from typing import Any, Dict, List, Optional

from .recorder.wal_ring import WalRingBuffer

GENESIS_MCP_SERVERS = (
    "blackbox-mcp",
    "pgsafe-mcp",
    "sysflow-mcp",
    "crawlengine-mcp",
    "browserbridge-mcp",
    "omnibridge-mcp",
    "fastmcp-forge",
)

CLIENT_DEFAULT_PATHS = {
    "cursor": os.path.join(".cursor", "mcp.json"),
    "claude": "claude_desktop_config.json",
    "windsurf": os.path.join(".windsurf", "mcp_config.json"),
}


class GenesisStackManager:
    """Provisions unified Genesis MCP stack configs and diagnoses cross-server WAL telemetry."""

    @staticmethod
    def build_stack_entries(db_path: str = ".blackbox/wal.sqlite") -> Dict[str, Dict[str, Any]]:
        abs_db = os.path.abspath(db_path)
        shared_env = {"BLACKBOX_DB_PATH": abs_db}
        return {
            "blackbox": {
                "command": "uvx",
                "args": ["--from", "genesis-blackbox", "blackbox-mcp", "--db", abs_db],
                "env": dict(shared_env),
            },
            "pgsafe-mcp": {
                "command": "uvx",
                "args": ["pgsafe-mcp"],
                "env": dict(shared_env),
            },
            "sysflow-mcp": {
                "command": "uvx",
                "args": ["sysflow-mcp"],
                "env": dict(shared_env),
            },
            "crawlengine-mcp": {
                "command": "uvx",
                "args": ["crawlengine-mcp"],
                "env": dict(shared_env),
            },
            "browserbridge-mcp": {
                "command": "uvx",
                "args": ["browserbridge-mcp"],
                "env": dict(shared_env),
            },
            "omnibridge-mcp": {
                "command": "uvx",
                "args": ["omnibridge-mcp"],
                "env": dict(shared_env),
            },
            "fastmcp-forge": {
                "command": "uvx",
                "args": ["fastmcp-forge", "serve"],
                "env": dict(shared_env),
            },
        }

    def init_stack_config(
        self,
        client: str = "cursor",
        output_path: Optional[str] = None,
        db_path: str = ".blackbox/wal.sqlite",
    ) -> Dict[str, Any]:
        target_client = client if client in CLIENT_DEFAULT_PATHS else "cursor"
        dest = output_path or CLIENT_DEFAULT_PATHS[target_client]
        abs_dest = os.path.abspath(dest)
        abs_db = os.path.abspath(db_path)

        existing: Dict[str, Any] = {}
        if os.path.isfile(abs_dest):
            try:
                with open(abs_dest, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    existing = loaded
            except Exception:
                existing = {}

        if not isinstance(existing.get("mcpServers"), dict):
            existing["mcpServers"] = {}

        entries = self.build_stack_entries(db_path=abs_db)
        for name, cfg in entries.items():
            existing["mcpServers"][name] = cfg

        parent = os.path.dirname(abs_dest)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(abs_dest, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
            f.write("\n")

        return {
            "client": target_client,
            "config_path": abs_dest,
            "db_path": abs_db,
            "servers_configured": list(entries.keys()),
        }

    def diagnose_ecosystem(
        self,
        db_path: str = ".blackbox/wal.sqlite",
        limit: int = 500,
    ) -> Dict[str, Any]:
        with WalRingBuffer(db_path=db_path) as ring:
            events = ring.query_recent(limit=max(1, int(limit)))

        by_source: Dict[str, Dict[str, Any]] = {}
        operational_incidents: List[Dict[str, Any]] = []
        total_errors = 0
        total_security_blocks = 0

        for ev in events:
            src = str(ev.get("source") or "unknown")
            etype = str(ev.get("event_type") or "EVENT")
            status = str(ev.get("status") or "ok")
            dur = ev.get("duration_ms")
            payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}

            info = by_source.setdefault(
                src,
                {
                    "source": src,
                    "total_events": 0,
                    "error_events": 0,
                    "security_blocks": 0,
                    "durations": [],
                    "event_types": set(),
                    "latest_error": None,
                },
            )
            info["total_events"] += 1
            info["event_types"].add(etype)
            if isinstance(dur, (int, float)):
                info["durations"].append(float(dur))

            err_text = str(payload.get("error") or "")
            is_block = bool(payload.get("blocked")) or any(
                marker in err_text
                for marker in (
                    "Security Guard Blocked",
                    "Blocked URL",
                    "Blocked outbound",
                    "SYSFLOW_ALLOWED_COMMANDS",
                    "Shell metacharacters",
                )
            )
            if is_block:
                info["security_blocks"] += 1
                total_security_blocks += 1

            if status == "error" or etype == "EXCEPTION":
                info["error_events"] += 1
                total_errors += 1
                summary = (
                    err_text
                    or str(payload.get("exception_message") or "")
                    or str(payload.get("query") or payload.get("command") or etype)
                )
                if info["latest_error"] is None:
                    info["latest_error"] = summary
                operational_incidents.append(
                    {
                        "id": ev.get("id"),
                        "timestamp": ev.get("timestamp"),
                        "source": src,
                        "event_type": etype,
                        "tool": payload.get("tool"),
                        "blocked": is_block,
                        "error": summary,
                    }
                )

        servers_summary: Dict[str, Dict[str, Any]] = {}
        for src, raw in sorted(by_source.items()):
            durs = raw["durations"]
            total_ev = raw["total_events"]
            err_ev = raw["error_events"]
            servers_summary[src] = {
                "total_events": total_ev,
                "error_events": err_ev,
                "error_rate": round(err_ev / total_ev, 4) if total_ev else 0.0,
                "security_blocks": raw["security_blocks"],
                "avg_duration_ms": round(sum(durs) / len(durs), 3) if durs else 0.0,
                "max_duration_ms": round(max(durs), 3) if durs else 0.0,
                "event_types": sorted(raw["event_types"]),
                "latest_error": raw["latest_error"],
            }

        report: Dict[str, Any] = {
            "db_path": db_path,
            "total_events_analyzed": len(events),
            "total_errors": total_errors,
            "total_security_blocks": total_security_blocks,
            "active_sources_count": len(servers_summary),
            "servers": servers_summary,
            "operational_incidents": operational_incidents[:10],
        }
        report["markdown_report"] = self._render_markdown(report)
        return report

    @staticmethod
    def _render_markdown(report: Dict[str, Any]) -> str:
        lines = [
            "# Genesis MCP Ecosystem Diagnostic Report",
            "",
            f"- **WAL Database:** `{report.get('db_path')}`",
            f"- **Events Analyzed:** `{report.get('total_events_analyzed', 0)}`",
            f"- **Active Sources:** `{report.get('active_sources_count', 0)}`",
            f"- **Operational Errors:** `{report.get('total_errors', 0)}`",
            f"- **Security Guard Blocks:** `{report.get('total_security_blocks', 0)}`",
            "",
        ]
        servers = report.get("servers") or {}
        if servers:
            lines.append("## Per-Server Telemetry")
            lines.append("| Source | Events | Errors | Blocks | Avg Latency | Max Latency | Event Types |")
            lines.append("|---|---|---|---|---|---|---|")
            for src, st in servers.items():
                etypes = ", ".join(st.get("event_types", []))
                lines.append(
                    f"| `{src}` | `{st['total_events']}` | `{st['error_events']}` | `{st['security_blocks']}` | "
                    f"`{st['avg_duration_ms']:.2f}ms` | `{st['max_duration_ms']:.2f}ms` | `{etypes}` |"
                )
            lines.append("")
        return "\n".join(lines)

