"""
Genesis Blackbox: Unified MCP Stack Provisioner & Ecosystem Diagnostics
Configures multi-server MCP environments sharing a single SQLite WAL flight recorder
and aggregates operational health across connected Genesis MCP servers.
"""

import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
from typing import Any, Dict, List, Optional

from .recorder.baseline import BaselineManager
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
    "claude-code": ".mcp.json",
    "windsurf": os.path.join(".windsurf", "mcp_config.json"),
    "vscode": os.path.join(".vscode", "mcp.json"),
    "antigravity": os.path.join(".gemini", "antigravity", "mcp_config.json"),
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

        if target_client == "vscode":
            root_key = "servers"
        else:
            root_key = "mcpServers"

        if not isinstance(existing.get(root_key), dict):
            existing[root_key] = {}

        entries = self.build_stack_entries(db_path=abs_db)
        for name, cfg in entries.items():
            existing[root_key][name] = cfg

        parent = os.path.dirname(abs_dest)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(abs_dest, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
            f.write("\n")

        return {
            "client": target_client,
            "root_key": root_key,
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

    def run_doctor(
        self,
        db_path: str = ".blackbox/wal.sqlite",
        root_dir: str = ".",
    ) -> Dict[str, Any]:
        """Runs comprehensive environment, WAL, MCP stack, and project integration health checks."""
        abs_root = os.path.abspath(root_dir)
        abs_db = os.path.abspath(db_path) if db_path != ":memory:" else ":memory:"
        baseline_path = BaselineManager.default_baseline_path(abs_db)

        node_bin = shutil.which("node")
        node_version = None
        if node_bin:
            try:
                proc = subprocess.run([node_bin, "--version"], capture_output=True, text=True, timeout=3)
                if proc.returncode == 0:
                    node_version = proc.stdout.strip()
            except Exception:
                node_version = None

        git_bin = shutil.which("git")
        git_repo = False
        if git_bin and os.path.isdir(abs_root):
            try:
                proc = subprocess.run(
                    [git_bin, "-C", abs_root, "rev-parse", "--is-inside-work-tree"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                git_repo = proc.returncode == 0 and proc.stdout.strip() == "true"
            except Exception:
                git_repo = False

        python_ok = sys.version_info >= (3, 10)
        runtime_info = {
            "python_version": sys.version.split()[0],
            "python_supported": python_ok,
            "os": platform.platform(),
            "node_available": bool(node_bin),
            "node_version": node_version,
            "git_available": bool(git_bin),
            "git_repo": git_repo,
        }

        wal_exists = abs_db != ":memory:" and os.path.isfile(abs_db)
        wal_quick_check = "not_created"
        wal_event_count = 0
        wal_ok = True
        if wal_exists:
            conn = None
            try:
                conn = sqlite3.connect(abs_db, timeout=5.0)
                qc = conn.execute("PRAGMA quick_check;").fetchone()
                wal_quick_check = str(qc[0]) if qc else "unknown"
                tbl = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='blackbox_events';"
                ).fetchone()
                if tbl:
                    row = conn.execute("SELECT COUNT(*) FROM blackbox_events;").fetchone()
                    wal_event_count = int(row[0]) if row else 0
                wal_ok = wal_quick_check == "ok"
            except sqlite3.Error as exc:
                wal_quick_check = f"error: {exc}"
                wal_ok = False
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except sqlite3.Error:
                        pass
        elif abs_db != ":memory:":
            parent_dir = os.path.dirname(abs_db) or abs_root
            probe = parent_dir
            while probe and not os.path.exists(probe):
                next_probe = os.path.dirname(probe)
                if next_probe == probe:
                    break
                probe = next_probe
            wal_ok = os.access(probe or abs_root, os.W_OK)

        wal_info = {
            "db_path": abs_db,
            "exists": wal_exists,
            "quick_check": wal_quick_check,
            "event_count": wal_event_count,
            "baseline_path": baseline_path,
            "baseline_exists": os.path.isfile(baseline_path),
            "healthy": wal_ok,
        }

        mcp_servers: Dict[str, Dict[str, Any]] = {}
        repo_search_roots = [
            os.path.join(abs_root, "build", "repos"),
            os.path.join(os.path.dirname(abs_root), "build", "repos"),
        ]
        for srv in GENESIS_MCP_SERVERS:
            bin_path = shutil.which(srv)
            local_repo = None
            if srv == "blackbox-mcp":
                for cand in (abs_root, os.path.join(abs_root, "blackbox")):
                    if os.path.isfile(os.path.join(cand, "pyproject.toml")):
                        local_repo = cand
                        break
            else:
                for base in repo_search_roots:
                    cand = os.path.join(base, srv)
                    if os.path.isdir(cand):
                        local_repo = cand
                        break
            mcp_servers[srv] = {
                "installed_on_path": bin_path is not None,
                "binary": bin_path,
                "local_repo": local_repo,
            }

        rule_files = {
            name: os.path.isfile(os.path.join(abs_root, name))
            for name in (".cursorrules", "CLAUDE.md", ".windsurfrules", "GEMINI.md")
        }
        mcp_configs = {
            client: {
                "path": os.path.join(abs_root, rel_path),
                "exists": os.path.isfile(os.path.join(abs_root, rel_path)),
            }
            for client, rel_path in CLIENT_DEFAULT_PATHS.items()
        }

        warnings: List[str] = []
        if not python_ok:
            warnings.append("Python >= 3.10 is required.")
        if not wal_ok:
            warnings.append(f"WAL health check issue: {wal_quick_check}")
        if not any(v["exists"] for v in mcp_configs.values()):
            warnings.append("No MCP client stack configuration found (run `blackbox init-stack`).")

        status = "healthy" if (python_ok and wal_ok) else "warnings"
        report: Dict[str, Any] = {
            "status": status,
            "root_dir": abs_root,
            "runtime": runtime_info,
            "wal": wal_info,
            "mcp_servers": mcp_servers,
            "agent_rules": rule_files,
            "mcp_configs": mcp_configs,
            "warnings": warnings,
        }
        report["markdown_report"] = self._render_doctor_markdown(report)
        return report

    @staticmethod
    def _render_doctor_markdown(report: Dict[str, Any]) -> str:
        rt = report["runtime"]
        wal = report["wal"]
        lines = [
            "# Genesis Blackbox Environment Doctor",
            "",
            f"- **Overall Status:** `{report['status']}`",
            f"- **Python:** `{rt['python_version']}` (`{'ok' if rt['python_supported'] else 'unsupported'}`)",
            f"- **OS:** `{rt['os']}`",
            f"- **Node.js:** `{rt['node_version'] or 'not found'}`",
            f"- **Git Repo:** `{'yes' if rt['git_repo'] else 'no'}`",
            f"- **WAL Database:** `{wal['db_path']}` (exists=`{wal['exists']}`, check=`{wal['quick_check']}`, events=`{wal['event_count']}`)",
            f"- **Baseline WAL:** `{wal['baseline_path']}` (exists=`{wal['baseline_exists']}`)",
            "",
            "## Genesis MCP Stack Availability",
            "| Server | On PATH | Local Repo |",
            "|---|---|---|",
        ]
        for srv, info in report["mcp_servers"].items():
            on_path = "yes" if info["installed_on_path"] else "no"
            local = info["local_repo"] or "-"
            lines.append(f"| `{srv}` | `{on_path}` | `{local}` |")
        if report["warnings"]:
            lines.append("")
            lines.append("## Notes & Recommendations")
            for w in report["warnings"]:
                lines.append(f"- {w}")
        lines.append("")
        return "\n".join(lines)

