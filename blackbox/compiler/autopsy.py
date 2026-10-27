"""
Genesis Blackbox: Causal Crash Compiler
Synthesizes runtime WAL ring-buffer events, deep exception state,
AST blast-radius graph, and git history to emit a deterministic `.blackbox.md` Autopsy Capsule.
"""

import os
import time
from datetime import datetime, timezone
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
        self.ast_graph = ast_graph or CodebaseGraph()

    def compile(
        self,
        exception_data: Optional[Dict[str, Any]] = None,
        recent_events_count: int = 25
    ) -> str:
        """
        Compiles .blackbox.md markdown capsule.
        If exception_data is None, attempts to extract the latest exception from WAL.
        """
        crash_timestamp: Optional[float] = None

        # 1. Resolve exception data from WAL if not provided
        if exception_data is None and self.wal_ring:
            recent_exceptions = self.wal_ring.query_recent(limit=5, event_type="EXCEPTION")
            if recent_exceptions:
                latest_exc_event = recent_exceptions[0]
                exception_data = latest_exc_event.get("payload")
                crash_timestamp = latest_exc_event.get("timestamp")

        if not exception_data:
            # Handle case where no exception was captured
            return self._compile_empty_or_healthy_capsule(recent_events_count)

        exc_type = exception_data.get("exception_type", "UnknownException")
        exc_msg = exception_data.get("exception_message", "")
        root_cause = exception_data.get("root_cause") or {}
        frames = exception_data.get("frames", [])

        if crash_timestamp is None:
            crash_timestamp = exception_data.get("timestamp", time.time())
        ts_str = datetime.fromtimestamp(crash_timestamp, tz=timezone.utc).isoformat()

        # 2. Extract recent execution events from flight recorder
        recent_events: List[Dict[str, Any]] = []
        if self.wal_ring:
            recent_events = self.wal_ring.query_recent(limit=recent_events_count)

        # Classify recent events
        http_events: List[Dict[str, Any]] = []
        sql_events: List[Dict[str, Any]] = []
        other_events: List[Dict[str, Any]] = []

        for e in recent_events:
            etype = e.get("event_type", "")
            if etype in ("HTTP_REQ", "HTTP_CLIENT", "HTTP_RESP"):
                http_events.append(e)
            elif etype in ("SQL_QUERY", "SQL_EXEC"):
                sql_events.append(e)
            elif etype != "EXCEPTION":
                other_events.append(e)

        # 3. Compute AST blast-radius
        blast_radius: Dict[str, Any] = {}
        target_fn = root_cause.get("function")
        culprit_file = root_cause.get("filename")

        if target_fn and self.ast_graph:
            blast_radius = self.ast_graph.get_blast_radius(
                target_function=target_fn,
                file_path=culprit_file if culprit_file and os.path.exists(culprit_file) else None
            )

        # 4. Extract recent git diff for involved files
        involved_files = []
        for f in frames:
            fname = f.get("filename")
            if fname and os.path.isfile(fname) and fname not in involved_files:
                involved_files.append(fname)

        git_diff_text = ""
        if involved_files:
            git_diff_text = self.ast_graph.get_git_diff(involved_files)

        # 5. Assemble .blackbox.md Markdown Capsule
        md = []
        md.append("# 🚨 BLACKBOX CRASH AUTOPSY CAPSULE")
        md.append(f"**Exception:** `{exc_type}`: {exc_msg}")
        md.append(f"**Timestamp (UTC):** `{ts_str}`")
        if culprit_file:
            md.append(f"**Culprit File:** `{culprit_file}` (Line {root_cause.get('lineno')})")
        if target_fn:
            md.append(f"**Culprit Function:** `{target_fn}()`")
        if root_cause.get("code_line"):
            md.append(f"**Culprit Statement:** `{root_cause.get('code_line').strip()}`")
        md.append("")

        # Section 1: Local Variables State
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
            md.append("*No local variable bindings captured for root cause frame.*")
        md.append("")

        # Section 2: AST Blast-Radius Impact
        if blast_radius and blast_radius.get("total_blast_radius", 0) > 0:
            md.append("## 2. AST Blast-Radius Impact")
            md.append(f"- **Target Function:** `{blast_radius.get('target')}`")
            upstream = blast_radius.get("upstream_callers", [])
            downstream = blast_radius.get("downstream_callees", [])
            md.append(f"- **Upstream Callers ({len(upstream)}):** {', '.join(f'`{c}`' for c in upstream) or 'None'}")
            md.append(f"- **Downstream Callees ({len(downstream)}):** {', '.join(f'`{c}`' for c in downstream) or 'None'}")
            md.append(f"- **Total Impacted Symbols:** `{blast_radius.get('total_blast_radius')}`")
            md.append("")

        # Section 3: Flight Recorder Timeline (Preceding Events)
        md.append("## 3. Flight Recorder Timeline (Preceding Events)")
        if http_events:
            md.append("### Recent HTTP Transactions")
            md.append("| Method | Path | Status | Latency | Source | Details |")
            md.append("|---|---|---|---|---|---|")
            for h in reversed(http_events[:8]):
                p = h.get("payload") or {}
                if isinstance(p, dict):
                    method = p.get("method", "GET")
                    path = p.get("path", p.get("url", "/"))
                    status = p.get("status_code", h.get("status", "ok"))
                    lat = f"{h.get('duration_ms', 0):.2f}ms" if h.get("duration_ms") is not None else "-"
                    summary = p.get("query", p.get("body_preview", "-"))
                else:
                    method = "REQ"
                    path = str(p)[:30]
                    status = h.get("status", "ok")
                    lat = "-"
                    summary = "-"
                md.append(f"| `{method}` | `{path}` | `{status}` | {lat} | `{h.get('source', '')}` | {summary} |")
            md.append("")

        if sql_events:
            md.append("### Preceding SQL Queries")
            md.append("| Status | Latency | Query | Bind Params (Sanitized) | Rows |")
            md.append("|---|---|---|---|---|")
            for s in reversed(sql_events[:8]):
                p = s.get("payload") or {}
                if isinstance(p, dict):
                    query = str(p.get("query", p.get("sql", ""))).replace("\n", " ").strip()
                    if len(query) > 60:
                        query = query[:57] + "..."
                    params = str(p.get("params", "-")).replace("\n", " ")
                    if len(params) > 40:
                        params = params[:37] + "..."
                    rows = p.get("rows_affected", "-")
                    lat = f"{s.get('duration_ms', 0):.2f}ms" if s.get("duration_ms") is not None else "-"
                else:
                    query = str(p)[:50]
                    params = "-"
                    rows = "-"
                    lat = "-"
                md.append(f"| `{s.get('status', 'ok')}` | {lat} | `{query}` | `{params}` | {rows} |")
            md.append("")

        if recent_events:
            md.append("### Chronological Event Stream")
            md.append("| Seq | Event Type | Source | Status | Payload |")
            md.append("|---|---|---|---|---|")
            for e in reversed(recent_events[:10]):
                eid = e.get("id")
                etype = e.get("event_type")
                esrc = e.get("source")
                estatus = e.get("status")
                payload = str(e.get("payload"))
                if len(payload) > 60:
                    payload = payload[:57] + "..."
                md.append(f"| #{eid} | `{etype}` | `{esrc}` | `{estatus}` | {payload} |")
            md.append("")
        else:
            md.append("*No preceding events found in WAL.*")
            md.append("")

        # Section 3: Traceback Stack Frames
        if len(frames) > 1:
            md.append("## 3. Execution Stack Trace Frames")
            for i, f in enumerate(frames):
                is_culprit = " (CRASH POINT)" if f == root_cause else ""
                md.append(f"### Frame {i + 1}: `{f.get('function')}()` in `{f.get('filename')}:{f.get('lineno')}`{is_culprit}")
                if f.get("code_line"):
                    md.append(f"```python\n{f.get('code_line').strip()}\n```")
                f_locals = f.get("locals", {})
                if f_locals and f != root_cause:
                    sample_locals = list(f_locals.items())[:5]
                    local_strs = [f"`{k}` = `{str(v)[:30]}`" for k, v in sample_locals]
                    md.append(f"- **Locals:** {', '.join(local_strs)}")
                md.append("")

        # Section 4: AST Blast-Radius Impact
        if blast_radius and blast_radius.get("total_blast_radius", 0) > 0:
            md.append("## 4. AST Blast-Radius Impact")
            md.append(f"- **Target Function:** `{blast_radius.get('target')}`")
            upstream = blast_radius.get("upstream_callers", [])
            downstream = blast_radius.get("downstream_callees", [])
            md.append(f"- **Upstream Callers ({len(upstream)}):** {', '.join(f'`{c}`' for c in upstream) or 'None'}")
            md.append(f"- **Downstream Callees ({len(downstream)}):** {', '.join(f'`{c}`' for c in downstream) or 'None'}")
            md.append(f"- **Total Impacted Symbols:** `{blast_radius.get('total_blast_radius')}`")
            md.append("")

        # Section 5: Recent Git Changes
        if git_diff_text:
            md.append("## 5. Recent Git Diff in Involved Files")
            md.append("```diff")
            md.append(git_diff_text)
            md.append("```")
            md.append("")

        # Section 6: Suggested Fix Mandate
        md.append("## 6. Suggested Fix Mandate")
        md.append("1. Validate incoming state and preconditions before executing the culprit statement.")
        if target_fn:
            md.append(f"2. Ensure any change to `{target_fn}` maintains compatibility with upstream callers: {', '.join(f'`{c}`' for c in blast_radius.get('upstream_callers', [])) or 'None'}.")
        md.append("3. Execute unit tests immediately to verify causal resolution without regressions.")
        md.append("")

        return "\n".join(md)

    def _compile_empty_or_healthy_capsule(self, recent_events_count: int) -> str:
        recent_events: List[Dict[str, Any]] = []
        if self.wal_ring:
            recent_events = self.wal_ring.query_recent(limit=recent_events_count)

        md = []
        md.append("# BLACKBOX EXECUTION TIMELINE (NO CRASH RECORDED)")
        md.append(f"**Total Recent Events:** {len(recent_events)}")
        md.append("")
        if recent_events:
            md.append("| ID | Event Type | Source | Status | Duration | Summary |")
            md.append("|---|---|---|---|---|---|")
            for e in reversed(recent_events):
                dur = f"{e.get('duration_ms', 0):.2f}ms" if e.get("duration_ms") is not None else "-"
                payload_str = str(e.get("payload", ""))
                if len(payload_str) > 60:
                    payload_str = payload_str[:57] + "..."
                md.append(f"| #{e.get('id')} | `{e.get('event_type')}` | `{e.get('source')}` | `{e.get('status')}` | {dur} | {payload_str} |")
        else:
            md.append("*No events recorded in flight recorder.*")
        md.append("")
        return "\n".join(md)

