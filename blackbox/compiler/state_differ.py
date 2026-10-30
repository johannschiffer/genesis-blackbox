"""
Genesis Blackbox: Time-Travel Execution Differ
Compares the last passing execution run against the latest failing run in WAL
to pinpoint the exact divergence in HTTP requests, SQL queries, and variable states.
"""

import os
import json
import time
import math
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple, Set
from ..recorder.wal_ring import WalRingBuffer


class StateDiffer:
    """
    Deterministic differential engine that analyzes execution runs recorded in WAL,
    aligns chronological event streams, and isolates the exact divergence point.
    """

    def __init__(self, wal_ring: Optional[WalRingBuffer] = None, db_path: Optional[str] = None):
        if wal_ring is not None:
            self.wal_ring = wal_ring
            self._owns_ring = False
        else:
            path = db_path or ".blackbox/wal.sqlite"
            self.wal_ring = WalRingBuffer(db_path=path)
            self._owns_ring = True

    def close(self) -> None:
        if self._owns_ring and self.wal_ring:
            self.wal_ring.close()

    def extract_runs(self) -> List[Dict[str, Any]]:
        """
        Groups recorded WAL events into execution sessions/runs based on
        lifecycle markers (PROCESS_START/EXIT), session IDs, trace IDs, or temporal boundaries.
        """
        timeline = self.wal_ring.get_timeline(limit=5000)
        if not timeline:
            return []

        runs: List[Dict[str, Any]] = []

        # Strategy 1: Lifecycle boundaries (PROCESS_START / SUBPROCESS_START)
        has_process_starts = any(
            e.get("event_type") in ("PROCESS_START", "SUBPROCESS_START")
            for e in timeline
        )
        if has_process_starts:
            current_run_events: List[Dict[str, Any]] = []
            current_run_id = None
            run_idx = 1
            for e in timeline:
                etype = e.get("event_type")
                p = e.get("payload") if isinstance(e.get("payload"), dict) else {}

                if etype in ("PROCESS_START", "SUBPROCESS_START"):
                    if current_run_events:
                        rid = current_run_id or f"run-{run_idx}"
                        runs.append(self._build_run_object(rid, current_run_events))
                        run_idx += 1
                        current_run_events = []
                    current_run_id = p.get("run_id") or p.get("session_id") or f"run-{run_idx}"
                    current_run_events.append(e)
                elif etype in ("PROCESS_EXIT", "SUBPROCESS_EXIT", "EXIT"):
                    current_run_events.append(e)
                    rid = current_run_id or p.get("run_id") or f"run-{run_idx}"
                    runs.append(self._build_run_object(rid, current_run_events))
                    run_idx += 1
                    current_run_events = []
                    current_run_id = None
                else:
                    current_run_events.append(e)

            if current_run_events:
                rid = current_run_id or f"run-{run_idx}"
                runs.append(self._build_run_object(rid, current_run_events))
            return runs

        # Strategy 2: Explicit run_id or session_id in payloads (when no PROCESS_START)
        has_explicit_runs = any(
            isinstance(e.get("payload"), dict) and ("run_id" in e["payload"] or "session_id" in e["payload"])
            for e in timeline
        )
        if has_explicit_runs:
            run_map: Dict[str, List[Dict[str, Any]]] = {}
            for e in timeline:
                p = e.get("payload") if isinstance(e.get("payload"), dict) else {}
                rid = p.get("run_id") or p.get("session_id") or "default_run"
                if rid not in run_map:
                    run_map[rid] = []
                run_map[rid].append(e)

            for rid, r_events in run_map.items():
                runs.append(self._build_run_object(rid, r_events))
            return runs

        # Strategy 3: Group by trace_id if present
        has_trace_ids = any(e.get("trace_id") for e in timeline)
        if has_trace_ids:
            trace_map: Dict[str, List[Dict[str, Any]]] = {}
            for e in timeline:
                tid = e.get("trace_id") or "no-trace"
                if tid not in trace_map:
                    trace_map[tid] = []
                trace_map[tid].append(e)
            for tid, t_events in trace_map.items():
                runs.append(self._build_run_object(tid, t_events))
            return runs

        # Strategy 4: Temporal gaps (> 2.0s) or EXCEPTION boundaries
        current_run_events = []
        run_idx = 1
        last_ts = None
        for e in timeline:
            ts = e.get("timestamp", 0)
            if last_ts is not None and (ts - last_ts > 2.0):
                if current_run_events:
                    runs.append(self._build_run_object(f"run-{run_idx}", current_run_events))
                    run_idx += 1
                    current_run_events = []
            current_run_events.append(e)
            if e.get("event_type") == "EXCEPTION":
                runs.append(self._build_run_object(f"run-{run_idx}", current_run_events))
                run_idx += 1
                current_run_events = []
            last_ts = ts

        if current_run_events:
            runs.append(self._build_run_object(f"run-{run_idx}", current_run_events))

        return runs

    def _build_run_object(self, run_id: str, events: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Classifies and aggregates metadata for an execution run."""
        start_ts = events[0].get("timestamp", 0) if events else 0
        end_ts = events[-1].get("timestamp", 0) if events else 0

        has_exception = False
        has_error_status = False
        exit_code = 0

        for e in events:
            if e.get("event_type") == "EXCEPTION":
                has_exception = True
            if e.get("status") == "error":
                has_error_status = True
            if e.get("event_type") in ("PROCESS_EXIT", "SUBPROCESS_EXIT", "EXIT"):
                p = e.get("payload") if isinstance(e.get("payload"), dict) else {}
                code = p.get("exit_code", p.get("returncode", 0))
                if code != 0:
                    exit_code = code

        status = "failing" if (has_exception or has_error_status or exit_code != 0) else "passing"

        return {
            "run_id": run_id,
            "status": status,
            "start_time": start_ts,
            "end_time": end_ts,
            "duration_ms": max(0.0, (end_ts - start_ts) * 1000.0),
            "event_count": len(events),
            "has_exception": has_exception,
            "exit_code": exit_code,
            "events": events
        }

    def resolve_runs(
        self,
        baseline_run_id: Optional[str] = None,
        failing_run_id: Optional[str] = None
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Resolves the baseline healthy run and crashing run."""
        runs = self.extract_runs()
        if not runs:
            raise ValueError("No execution runs found in WAL database.")

        baseline_run = None
        failing_run = None

        if baseline_run_id:
            for r in runs:
                if str(r["run_id"]) == str(baseline_run_id):
                    baseline_run = r
                    break
        else:
            # Last passing run
            for r in reversed(runs):
                if r["status"] == "passing":
                    baseline_run = r
                    break

        if failing_run_id:
            for r in runs:
                if str(r["run_id"]) == str(failing_run_id):
                    failing_run = r
                    break
        else:
            # Latest failing run
            for r in reversed(runs):
                if r["status"] == "failing":
                    failing_run = r
                    break

        if not baseline_run:
            raise ValueError("Could not find a baseline PASSING execution run in WAL.")
        if not failing_run:
            raise ValueError("Could not find a FAILING execution run in WAL.")

        return baseline_run, failing_run

    def diff(
        self,
        baseline_run_id: Optional[str] = None,
        failing_run_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Performs deep deterministic state diff between baseline run and failing run.
        """
        baseline_run, failing_run = self.resolve_runs(baseline_run_id, failing_run_id)

        b_events = baseline_run["events"]
        f_events = failing_run["events"]

        # Step 1: Sequence Alignment Diff
        alignment_diff, divergence_point = self._align_and_find_divergence(b_events, f_events)

        # Step 2: SQL Query Diff
        sql_diff = self._diff_sql_events(b_events, f_events)

        # Step 3: HTTP Transaction Diff
        http_diff = self._diff_http_events(b_events, f_events)

        # Step 4: Variable State Diff
        variable_diff = self._diff_variables_and_exception(baseline_run, failing_run)

        return {
            "baseline_run": {
                "run_id": baseline_run["run_id"],
                "status": baseline_run["status"],
                "timestamp": baseline_run["start_time"],
                "event_count": baseline_run["event_count"]
            },
            "failing_run": {
                "run_id": failing_run["run_id"],
                "status": failing_run["status"],
                "timestamp": failing_run["start_time"],
                "event_count": failing_run["event_count"]
            },
            "divergence_point": divergence_point,
            "alignment_diff": alignment_diff,
            "sql_diff": sql_diff,
            "http_diff": http_diff,
            "variable_diff": variable_diff
        }

    def compare_runs(
        self,
        baseline_run_id: Optional[str] = None,
        failing_run_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Alias for diff() returning structured divergence report."""
        return self.diff(baseline_run_id=baseline_run_id, failing_run_id=failing_run_id)

    def _align_and_find_divergence(
        self,
        b_events: List[Dict[str, Any]],
        f_events: List[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """
        Aligns the chronological event sequences and pinpoints the first divergence point.
        """
        alignment: List[Dict[str, Any]] = []
        divergence: Optional[Dict[str, Any]] = None

        max_len = max(len(b_events), len(f_events))

        for idx in range(max_len):
            b_ev = b_events[idx] if idx < len(b_events) else None
            f_ev = f_events[idx] if idx < len(f_events) else None

            step_num = idx + 1
            aligned = True
            diff_note = ""

            if b_ev and f_ev:
                b_type = b_ev.get("event_type")
                f_type = f_ev.get("event_type")
                b_stat = b_ev.get("status")
                f_stat = f_ev.get("status")

                if f_type == "EXCEPTION":
                    aligned = False
                    diff_note = f"Crash point: uncaught exception {f_ev.get('payload', {}).get('exception_type', '')}"
                    if not divergence:
                        divergence = {
                            "step": step_num,
                            "type": "UNCAUGHT_EXCEPTION",
                            "description": f"Crashing run threw {f_ev.get('payload', {}).get('exception_type')}: {f_ev.get('payload', {}).get('exception_message')}",
                            "baseline_event": b_ev,
                            "failing_event": f_ev
                        }
                elif b_stat == "ok" and f_stat == "error":
                    aligned = False
                    diff_note = f"Event failed with status=error (baseline was ok)"
                    if not divergence:
                        div_type = "SQL_QUERY_FAILURE" if f_type in ("SQL_QUERY", "SQL_EXEC") else ("HTTP_STATUS_FAILURE" if f_type in ("HTTP_REQ", "HTTP_RESP") else "STEP_ERROR")
                        divergence = {
                            "step": step_num,
                            "type": div_type,
                            "description": f"Event #{f_ev.get('id')} [{f_type}] failed in crashing run",
                            "baseline_event": b_ev,
                            "failing_event": f_ev
                        }
                elif b_type != f_type:
                    aligned = False
                    diff_note = f"Event type shift: {b_type} -> {f_type}"
                    if not divergence:
                        divergence = {
                            "step": step_num,
                            "type": "EVENT_SEQUENCE_SHIFT",
                            "description": f"Execution sequence diverged at step #{step_num}: expected {b_type}, got {f_type}",
                            "baseline_event": b_ev,
                            "failing_event": f_ev
                        }
                else:
                    # Compare payload shapes
                    p_diff = self._compare_payload_shapes(b_type, b_ev.get("payload"), f_ev.get("payload"))
                    if p_diff:
                        aligned = False
                        diff_note = p_diff
                        if not divergence:
                            div_type = "PARAM_TYPE_MUTATION" if "param" in p_diff.lower() else "SCHEMA_SHIFT"
                            divergence = {
                                "step": step_num,
                                "type": div_type,
                                "description": p_diff,
                                "baseline_event": b_ev,
                                "failing_event": f_ev
                            }
            elif f_ev and not b_ev:
                aligned = False
                diff_note = f"Extra event in crashing run: [{f_ev.get('event_type')}]"
                if not divergence:
                    divergence = {
                        "step": step_num,
                        "type": "EXTRA_EVENT",
                        "description": f"Crashing run emitted extra event #{f_ev.get('id')} [{f_ev.get('event_type')}]",
                        "baseline_event": None,
                        "failing_event": f_ev
                    }
            elif b_ev and not f_ev:
                aligned = False
                diff_note = f"Premature termination: baseline had [{b_ev.get('event_type')}]"
                if not divergence:
                    divergence = {
                        "step": step_num,
                        "type": "PREMATURE_TERMINATION",
                        "description": f"Crashing run terminated before executing #{b_ev.get('id')} [{b_ev.get('event_type')}]",
                        "baseline_event": b_ev,
                        "failing_event": None
                    }

            alignment.append({
                "step": step_num,
                "aligned": aligned,
                "baseline": f"#{b_ev.get('id')} [{b_ev.get('event_type')}] ({b_ev.get('status')})" if b_ev else "None",
                "failing": f"#{f_ev.get('id')} [{f_ev.get('event_type')}] ({f_ev.get('status')})" if f_ev else "None",
                "diff_note": diff_note
            })

        return alignment, divergence

    def _compare_payload_shapes(self, event_type: str, b_payload: Any, f_payload: Any) -> Optional[str]:
        """Checks for bind param shape mutations or HTTP payload schema shifts."""
        if not isinstance(b_payload, dict) or not isinstance(f_payload, dict):
            return None

        # SQL checks
        if event_type in ("SQL_QUERY", "SQL_EXEC", "sql_query"):
            b_sql = str(b_payload.get("query", b_payload.get("sql", ""))).strip()
            f_sql = str(f_payload.get("query", f_payload.get("sql", ""))).strip()
            b_norm = " ".join(b_sql.split())
            f_norm = " ".join(f_sql.split())
            if b_norm != f_norm:
                return f"SQL query structure changed: '{b_sql[:35]}' vs '{f_sql[:35]}'"

            b_params = b_payload.get("params")
            f_params = f_payload.get("params")
            if b_params is not None and f_params is not None:
                shape_diff = self._diff_param_shapes(b_params, f_params)
                if shape_diff:
                    return f"SQL bind param shape mutation: {shape_diff}"

        # HTTP checks
        if event_type in ("HTTP_REQ", "HTTP_CLIENT", "HTTP_RESP"):
            b_path = b_payload.get("path", b_payload.get("url"))
            f_path = f_payload.get("path", f_payload.get("url"))
            if b_path != f_path:
                return f"HTTP target path changed: '{b_path}' vs '{f_path}'"

            # Check JSON body keys
            b_body = b_payload.get("body") or b_payload.get("json") or b_payload.get("payload")
            f_body = f_payload.get("body") or f_payload.get("json") or f_payload.get("payload")
            if isinstance(b_body, dict) and isinstance(f_body, dict):
                b_keys = set(b_body.keys())
                f_keys = set(f_body.keys())
                missing = b_keys - f_keys
                extra = f_keys - b_keys
                if missing:
                    return f"HTTP payload missing expected key(s): {sorted(list(missing))}"
                if extra:
                    return f"HTTP payload added new key(s): {sorted(list(extra))}"

        return None

    def _is_nan(self, val: Any) -> bool:
        """Checks if a value is float NaN."""
        return isinstance(val, float) and math.isnan(val)

    def _diff_param_shapes(self, b_params: Any, f_params: Any, depth: int = 0, max_depth: int = 5) -> Optional[str]:
        """Detects parameter type mutation such as value mutating to None, NaN, or nested schema shift."""
        if depth > max_depth:
            return None

        # NaN transitions
        b_nan = self._is_nan(b_params)
        f_nan = self._is_nan(f_params)
        if b_nan and not f_nan:
            return f"Param mutated from NaN to {type(f_params).__name__}"
        if not b_nan and f_nan:
            return f"Param ({type(b_params).__name__}) mutated to NaN"
        if b_nan and f_nan:
            return None

        if type(b_params) is not type(f_params) and (b_params is not None and f_params is not None):
            return f"Type mismatch: {type(b_params).__name__} vs {type(f_params).__name__}"

        if isinstance(b_params, (list, tuple)) and isinstance(f_params, (list, tuple)):
            if len(b_params) != len(f_params):
                return f"Parameter length changed: {len(b_params)} vs {len(f_params)}"
            for i, (bp, fp) in enumerate(zip(b_params, f_params)):
                bp_nan = self._is_nan(bp)
                fp_nan = self._is_nan(fp)
                if bp_nan and not fp_nan:
                    return f"Param #{i} mutated from NaN to {type(fp).__name__}"
                if not bp_nan and fp_nan:
                    return f"Param #{i} ({type(bp).__name__}) mutated to NaN"
                if bp_nan and fp_nan:
                    continue

                if bp is not None and fp is None:
                    return f"Param #{i} ({type(bp).__name__}) mutated to None"
                if bp is None and fp is not None:
                    return f"Param #{i} mutated from None to {type(fp).__name__}"
                if type(bp) is not type(fp) and bp is not None and fp is not None:
                    return f"Param #{i} mutated from {type(bp).__name__} to {type(fp).__name__}"

                if isinstance(bp, (dict, list, tuple)) and isinstance(fp, (dict, list, tuple)):
                    nested = self._diff_param_shapes(bp, fp, depth + 1, max_depth)
                    if nested:
                        return f"Param #{i} -> {nested}"

        elif isinstance(b_params, dict) and isinstance(f_params, dict):
            for k, bv in b_params.items():
                if k not in f_params:
                    return f"Named param '{k}' missing in crashing run"
                fv = f_params[k]
                bv_nan = self._is_nan(bv)
                fv_nan = self._is_nan(fv)
                if bv_nan and not fv_nan:
                    return f"Named param '{k}' mutated from NaN to {type(fv).__name__}"
                if not bv_nan and fv_nan:
                    return f"Named param '{k}' ({type(bv).__name__}) mutated to NaN"
                if bv_nan and fv_nan:
                    continue

                if bv is not None and fv is None:
                    return f"Named param '{k}' mutated to None"
                if bv is None and fv is not None:
                    return f"Named param '{k}' mutated from None to {type(fv).__name__}"
                if type(bv) is not type(fv) and bv is not None and fv is not None:
                    return f"Named param '{k}' mutated from {type(bv).__name__} to {type(fv).__name__}"

                if isinstance(bv, (dict, list, tuple)) and isinstance(fv, (dict, list, tuple)):
                    nested = self._diff_param_shapes(bv, fv, depth + 1, max_depth)
                    if nested:
                        return f"Named param '{k}' -> {nested}"

            for k in f_params:
                if k not in b_params:
                    return f"Named param '{k}' added in crashing run"

        return None

    def _diff_sql_events(
        self,
        b_events: List[Dict[str, Any]],
        f_events: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Extracts and compares all SQL queries."""
        b_sql = [e for e in b_events if e.get("event_type") in ("SQL_QUERY", "SQL_EXEC", "sql_query")]
        f_sql = [e for e in f_events if e.get("event_type") in ("SQL_QUERY", "SQL_EXEC", "sql_query")]

        diffs = []
        max_sql = max(len(b_sql), len(f_sql))

        for i in range(max_sql):
            bs = b_sql[i] if i < len(b_sql) else None
            fs = f_sql[i] if i < len(f_sql) else None

            bp = bs.get("payload", {}) if bs and isinstance(bs.get("payload"), dict) else {}
            fp = fs.get("payload", {}) if fs and isinstance(fs.get("payload"), dict) else {}

            diff_item = {
                "step": i + 1,
                "baseline_query": bp.get("query", bp.get("sql", "-")),
                "failing_query": fp.get("query", fp.get("sql", "-")),
                "baseline_status": bs.get("status", "-") if bs else "None",
                "failing_status": fs.get("status", "-") if fs else "None",
                "baseline_params": bp.get("params", "-"),
                "failing_params": fp.get("params", "-"),
                "divergence": None
            }

            if bs and fs:
                if bs.get("status") == "ok" and fs.get("status") == "error":
                    diff_item["divergence"] = f"Query failed in crashing run: {fp.get('error', 'Execution Error')}"
                else:
                    b_norm = " ".join(str(bp.get("query", bp.get("sql", ""))).split())
                    f_norm = " ".join(str(fp.get("query", fp.get("sql", ""))).split())
                    if b_norm != f_norm:
                        diff_item["divergence"] = "SQL query structure changed"
                    else:
                        param_shape_diff = self._diff_param_shapes(bp.get("params"), fp.get("params"))
                        if param_shape_diff:
                            diff_item["divergence"] = param_shape_diff
            elif fs and not bs:
                diff_item["divergence"] = "New unexpected query executed in crashing run"
            elif bs and not fs:
                diff_item["divergence"] = "Query omitted in crashing run"

            diffs.append(diff_item)

        return diffs

    def _diff_http_events(
        self,
        b_events: List[Dict[str, Any]],
        f_events: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Extracts and compares all HTTP requests/responses."""
        b_http = [e for e in b_events if e.get("event_type") in ("HTTP_REQ", "HTTP_CLIENT", "HTTP_RESP", "http_transaction")]
        f_http = [e for e in f_events if e.get("event_type") in ("HTTP_REQ", "HTTP_CLIENT", "HTTP_RESP", "http_transaction")]

        diffs = []
        max_http = max(len(b_http), len(f_http))

        for i in range(max_http):
            bh = b_http[i] if i < len(b_http) else None
            fh = f_http[i] if i < len(f_http) else None

            bp = bh.get("payload", {}) if bh and isinstance(bh.get("payload"), dict) else {}
            fp = fh.get("payload", {}) if fh and isinstance(fh.get("payload"), dict) else {}

            diff_item = {
                "step": i + 1,
                "baseline_endpoint": f"{bp.get('method', 'REQ')} {bp.get('path', bp.get('url', '-'))}",
                "failing_endpoint": f"{fp.get('method', 'REQ')} {fp.get('path', fp.get('url', '-'))}",
                "baseline_status": bp.get("status_code", bh.get("status", "-") if bh else "-"),
                "failing_status": fp.get("status_code", fh.get("status", "-") if fh else "-"),
                "divergence": None
            }

            if bh and fh:
                b_sc = bp.get("status_code", 200)
                f_sc = fp.get("status_code", 200)
                if b_sc != f_sc:
                    diff_item["divergence"] = f"HTTP status code shifted: {b_sc} -> {f_sc}"
                else:
                    # Check body keys
                    b_b = bp.get("body") or bp.get("payload")
                    f_b = fp.get("body") or fp.get("payload")
                    if isinstance(b_b, dict) and isinstance(f_b, dict):
                        b_keys = set(b_b.keys())
                        f_keys = set(f_b.keys())
                        if b_keys != f_keys:
                            diff_item["divergence"] = f"Payload schema shift: missing {sorted(list(b_keys - f_keys))}, added {sorted(list(f_keys - b_keys))}"
            elif fh and not bh:
                diff_item["divergence"] = "Unexpected HTTP transaction in crashing run"
            elif bh and not fh:
                diff_item["divergence"] = "HTTP transaction omitted in crashing run"

            diffs.append(diff_item)

        return diffs

    def _diff_variables_and_exception(
        self,
        baseline_run: Dict[str, Any],
        failing_run: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Extracts variable mutations leading up to crash."""
        f_events = failing_run["events"]
        exc_event = None
        for e in f_events:
            if e.get("event_type") == "EXCEPTION":
                exc_event = e
                break

        if not exc_event:
            return {"exception": None, "mutated_variables": {}}

        payload = exc_event.get("payload", {})
        root_cause = payload.get("root_cause", {})
        locals_dict = root_cause.get("locals", {})

        mutated: Dict[str, Any] = {}
        for var_name, val in locals_dict.items():
            if val is None:
                mutated[var_name] = {"crashing_value": "None", "issue": "Value mutated to None"}
            elif str(val).startswith("<undefined"):
                mutated[var_name] = {"crashing_value": "undefined", "issue": "Variable undefined in V8 scope"}
            elif isinstance(val, (int, float, str, bool, dict, list)):
                mutated[var_name] = {"crashing_value": str(val)[:50], "issue": "Captured at crash point"}

        return {
            "exception": {
                "type": payload.get("exception_type"),
                "message": payload.get("exception_message"),
                "culprit_file": root_cause.get("filename"),
                "culprit_line": root_cause.get("lineno"),
                "culprit_function": root_cause.get("function"),
                "culprit_statement": root_cause.get("code_line")
            },
            "mutated_variables": mutated
        }

    @staticmethod
    def _escape_md_cell(val: Any) -> str:
        return str(val).replace("\r", " ").replace("\n", " ").replace("|", "\\|").strip()

    def compile_markdown(self, diff_result: Dict[str, Any]) -> str:
        """
        Compiles .blackbox-diff.md deterministic report for AI coding agents.
        """
        b_run = diff_result["baseline_run"]
        f_run = diff_result["failing_run"]
        div = diff_result.get("divergence_point")

        try:
            b_ts = datetime.fromtimestamp(float(b_run["timestamp"]), tz=timezone.utc).isoformat() if b_run.get("timestamp") else "N/A"
        except (ValueError, TypeError, OSError):
            b_ts = str(b_run.get("timestamp") or "N/A")

        try:
            f_ts = datetime.fromtimestamp(float(f_run["timestamp"]), tz=timezone.utc).isoformat() if f_run.get("timestamp") else "N/A"
        except (ValueError, TypeError, OSError):
            f_ts = str(f_run.get("timestamp") or "N/A")

        md = []
        md.append("# ⏱️ BLACKBOX TIME-TRAVEL EXECUTION DIFF")
        md.append(f"**Baseline Run (PASSING):** `{b_run['run_id']}` ({b_ts})")
        md.append(f"**Crashing Run (FAILING):** `{f_run['run_id']}` ({f_ts})")

        if div:
            md.append(f"**Divergence Point:** Step #{div['step']} (`{div['type']}`)")
            md.append(f"**Divergence Description:** {div['description']}")
        else:
            md.append("**Divergence Point:** No divergence detected between runs.")
        md.append("")

        # Section 1: Divergence Root Cause
        md.append("## 1. Divergence Root Cause Analysis")
        if div:
            md.append(f"- **Step Index:** `#{div['step']}`")
            md.append(f"- **Classification:** `{div['type']}`")
            md.append(f"- **Details:** {div['description']}")
            if div.get("baseline_event"):
                be = div["baseline_event"]
                md.append(f"- **Healthy Baseline State:** `#{be.get('id')} [{be.get('event_type')}] status={be.get('status')}`")
            if div.get("failing_event"):
                fe = div["failing_event"]
                md.append(f"- **Crashing State:** `#{fe.get('id')} [{fe.get('event_type')}] status={fe.get('status')}`")
        else:
            md.append("Both runs executed identically across all recorded checkpoints.")
        md.append("")

        # Section 2: SQL Query Divergence
        sql_diffs = diff_result.get("sql_diff", [])
        if sql_diffs:
            md.append("## 2. SQL Query Divergence")
            md.append("| Step | Healthy Baseline Query | Crashing Query | Status Shift | Divergence / Param Mutation |")
            md.append("|---|---|---|---|---|")
            for sq in sql_diffs:
                b_q = self._escape_md_cell(sq['baseline_query'])
                if len(b_q) > 40:
                    b_q = b_q[:37] + "..."
                f_q = self._escape_md_cell(sq['failing_query'])
                if len(f_q) > 40:
                    f_q = f_q[:37] + "..."
                status_shift = f"`{self._escape_md_cell(sq['baseline_status'])}` -> `{self._escape_md_cell(sq['failing_status'])}`"
                div_note = self._escape_md_cell(sq['divergence'] or "MATCH")
                md.append(f"| #{sq['step']} | `{b_q}` | `{f_q}` | {status_shift} | {div_note} |")
            md.append("")

        # Section 3: HTTP Transaction Divergence
        http_diffs = diff_result.get("http_diff", [])
        if http_diffs:
            md.append("## 3. HTTP Transaction Divergence")
            md.append("| Step | Healthy Endpoint | Crashing Endpoint | Status Code Shift | Divergence Details |")
            md.append("|---|---|---|---|---|")
            for hq in http_diffs:
                sc_shift = f"`{self._escape_md_cell(hq['baseline_status'])}` -> `{self._escape_md_cell(hq['failing_status'])}`"
                div_note = self._escape_md_cell(hq['divergence'] or "MATCH")
                b_ep = self._escape_md_cell(hq['baseline_endpoint'])
                f_ep = self._escape_md_cell(hq['failing_endpoint'])
                md.append(f"| #{hq['step']} | `{b_ep}` | `{f_ep}` | {sc_shift} | {div_note} |")
            md.append("")

        # Section 4: Variable State & Exception Divergence
        var_diff = diff_result.get("variable_diff", {})
        exc = var_diff.get("exception")
        mutated = var_diff.get("mutated_variables", {})

        if exc or mutated:
            md.append("## 4. Local Variable & Exception Divergence")
            if exc:
                md.append(f"**Uncaught Crash:** `{exc.get('type')}`: {exc.get('message')}")
                culprit_file = str(exc.get("culprit_file") or "")
                if culprit_file:
                    md.append(f"**Culprit Location:** `{culprit_file}:{exc.get('culprit_line')}` in `{exc.get('culprit_function')}()`")
                if exc.get("culprit_statement"):
                    if culprit_file.endswith((".ts", ".tsx")):
                        fence = "typescript"
                    elif culprit_file.endswith((".js", ".jsx", ".mjs", ".cjs")):
                        fence = "javascript"
                    else:
                        fence = "python"
                    md.append(f"```{fence}\n{exc.get('culprit_statement').strip()}\n```")
                md.append("")

            if mutated:
                md.append("### Variable Mutations at Divergence")
                md.append("| Variable | Crashing Run Value | Diagnosis |")
                md.append("|---|---|---|")
                for vk, vv in mutated.items():
                    md.append(
                        f"| `{self._escape_md_cell(vk)}` | `{self._escape_md_cell(vv.get('crashing_value'))}` | {self._escape_md_cell(vv.get('issue'))} |"
                    )
                md.append("")

        # Section 5: Chronological Alignment Stream
        alignments = diff_result.get("alignment_diff", [])
        if alignments:
            md.append("## 5. Event Sequence Alignment Timeline")
            md.append("| Step | Healthy Baseline Event | Crashing Run Event | Alignment | Diff Note |")
            md.append("|---|---|---|---|---|")
            for al in alignments[:25]:
                match_badge = "✅ MATCH" if al["aligned"] else "❌ DIVERGED"
                note = self._escape_md_cell(al["diff_note"] or "-")
                b_cell = self._escape_md_cell(al['baseline'])
                f_cell = self._escape_md_cell(al['failing'])
                md.append(f"| #{al['step']} | `{b_cell}` | `{f_cell}` | {match_badge} | {note} |")
            md.append("")

        # Section 6: Actionable Mandate
        md.append("## 6. Actionable Fix Mandate")
        if div:
            md.append(f"1. Fix the divergence at Step #{div['step']} (`{div['type']}`): {div['description']}.")
        md.append("2. Guard against missing fields or None mutations prior to the failing statement.")
        md.append("3. Re-run execution verification to confirm healthy baseline parity.")
        md.append("")

        return "\n".join(md)
