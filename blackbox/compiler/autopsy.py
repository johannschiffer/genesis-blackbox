"""
Genesis Blackbox: Causal Crash Compiler
Synthesizes runtime WAL ring-buffer events, deep exception state,
and AST blast-radius graph to emit a deterministic `.blackbox.md` Autopsy Capsule.
"""

from typing import Dict, Any, List, Optional
from ..recorder.wal_ring import WalRingBuffer
from ..ast_engine.graph import CodebaseGraph


class CausalCrashCompiler:
    """
    Compiles deterministic, token-compact autopsy reports engineered
    for autonomous AI coding agents to fix complex failures on the first attempt.
    """

    def __init__(
        self,
        wal_ring: Optional[WalRingBuffer] = None,
        ast_graph: Optional[CodebaseGraph] = None
    ):
        self.wal_ring = wal_ring
        self.ast_graph = ast_graph

    def compile(
        self,
        exception_data: Dict[str, Any],
        recent_events_count: int = 15
    ) -> str:
        exc_type = exception_data.get("exception_type", "UnknownException")
        exc_msg = exception_data.get("exception_message", "")
        root_cause = exception_data.get("root_cause") or {}
        frames = exception_data.get("frames", [])

        # 1. Fetch recent events from flight recorder
        recent_events: List[Dict[str, Any]] = []
        if self.wal_ring:
            recent_events = self.wal_ring.query_recent(limit=recent_events_count)

        # 2. Compute AST blast-radius if function known
        blast_radius: Dict[str, Any] = {}
        target_fn = root_cause.get("function")
        if target_fn and self.ast_graph:
            blast_radius = self.ast_graph.get_blast_radius(target_fn)

        # 3. Assemble .blackbox.md Markdown Capsule
        md = []
        md.append("# 🚨 BLACKBOX CRASH AUTOPSY CAPSULE")
        md.append(f"**Exception:** `{exc_type}`: {exc_msg}")
        md.append(f"**Culprit File:** `{root_cause.get('filename')}` (Line {root_cause.get('lineno')})")
        md.append(f"**Culprit Function:** `{root_cause.get('function')}()`")
        if root_cause.get("code_line"):
            md.append(f"**Culprit Statement:** `{root_cause.get('code_line')}`")
        md.append("")

        md.append("## 1. Local Variable State at Crash Time")
        locals_dict = root_cause.get("locals", {})
        if locals_dict:
            md.append("| Variable | Value |")
            md.append("|---|---|")
            for k, v in locals_dict.items():
                val_str = str(v).replace("\n", " ")
                if len(val_str) > 80:
                    val_str = val_str[:77] + "..."
                md.append(f"| `{k}` | `{val_str}` |")
        else:
            md.append("*No local variable bindings captured.*")
        md.append("")

        if blast_radius:
            md.append("## 2. AST Blast-Radius Impact")
            md.append(f"- **Target Function:** `{blast_radius.get('target')}`")
            md.append(f"- **Upstream Callers ({len(blast_radius.get('upstream_callers', []))}):** {', '.join(f'`{c}`' for c in blast_radius.get('upstream_callers', [])) or 'None'}")
            md.append(f"- **Downstream Callees ({len(blast_radius.get('downstream_callees', []))}):** {', '.join(f'`{c}`' for c in blast_radius.get('downstream_callees', [])) or 'None'}")
            md.append("")

        if recent_events:
            md.append("## 3. Flight Recorder Timeline (Preceding Events)")
            md.append("| Seq | Event Type | Source | Status | Summary |")
            md.append("|---|---|---|---|---|")
            for e in reversed(recent_events):
                eid = e.get("id")
                etype = e.get("event_type")
                esrc = e.get("source")
                estatus = e.get("status")
                payload = str(e.get("payload"))
                if len(payload) > 50:
                    payload = payload[:47] + "..."
                md.append(f"| #{eid} | `{etype}` | `{esrc}` | `{estatus}` | {payload} |")
            md.append("")

        md.append("## 4. Suggested Fix Mandate")
        md.append("1. Validate incoming state before executing the culprit statement.")
        md.append(f"2. Ensure any change to `{root_cause.get('function')}` does not regress upstream callers.")
        md.append("3. Execute test suite immediately to verify causal resolution.")
        md.append("")

        return "\n".join(md)
