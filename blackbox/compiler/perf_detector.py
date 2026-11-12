"""
Genesis Blackbox: N+1 SQL Query & Latency Regression Detector
Detects repetitive structurally identical SQL queries within a run/trace_id
and identifies significant latency regressions against a sanitized baseline WAL.
"""

import os
import re
from statistics import median
from typing import Any, Dict, List, Optional, Tuple

from ..recorder.wal_ring import WalRingBuffer

_SQL_SINGLE_QUOTED = re.compile(r"'(?:\\.|''|[^'\\])*'")
_SQL_DOUBLE_QUOTED = re.compile(r'"(?:\\.|""|[^"\\])*"')
_SQL_NUMERIC_LITERAL = re.compile(r"\b\d+(?:\.\d+)?\b")

_SQL_EVENT_TYPES = frozenset({"SQL_QUERY", "SQL_EXEC", "sql_query"})
_HTTP_EVENT_TYPES = frozenset({"HTTP_REQ", "HTTP_CLIENT", "HTTP_RESP", "http_transaction"})
_SUBPROCESS_EVENT_TYPES = frozenset({"SUBPROCESS", "subprocess", "SUBPROCESS_START", "SUBPROCESS_EXIT"})


class PerformanceDetector:
    """Analyzes recorded WAL events for N+1 SQL anti-patterns and latency regressions."""

    @staticmethod
    def normalize_sql_fingerprint(sql: str) -> str:
        """Normalizes whitespace, quoted string literals, and numeric literals to `?`."""
        if not sql:
            return ""
        normalized = _SQL_SINGLE_QUOTED.sub("?", str(sql))
        normalized = _SQL_DOUBLE_QUOTED.sub("?", normalized)
        normalized = _SQL_NUMERIC_LITERAL.sub("?", normalized)
        return " ".join(normalized.split())

    @staticmethod
    def _extract_sql_text(event: Dict[str, Any]) -> str:
        payload = event.get("payload")
        if isinstance(payload, dict):
            return str(payload.get("query") or payload.get("sql") or "").strip()
        return str(payload or "").strip()

    @staticmethod
    def _extract_trace_id(event: Dict[str, Any]) -> Optional[str]:
        tid = event.get("trace_id")
        if tid:
            return str(tid)
        payload = event.get("payload")
        if isinstance(payload, dict) and payload.get("trace_id"):
            return str(payload["trace_id"])
        return None

    def detect_n_plus_one(self, events: List[Dict[str, Any]], threshold: int = 5) -> List[Dict[str, Any]]:
        """
        Detects normalized SQL fingerprints executed >= threshold times within the same
        trace_id (or current run scope).
        """
        if not events:
            return []
        min_count = max(2, int(threshold))

        grouped: Dict[Tuple[Optional[str], str], List[Dict[str, Any]]] = {}
        samples: Dict[Tuple[Optional[str], str], str] = {}

        for ev in events:
            if ev.get("event_type") not in _SQL_EVENT_TYPES:
                continue
            raw_sql = self._extract_sql_text(ev)
            if not raw_sql:
                continue
            fp = self.normalize_sql_fingerprint(raw_sql)
            tid = self._extract_trace_id(ev)
            key = (tid, fp)
            grouped.setdefault(key, []).append(ev)
            samples.setdefault(key, raw_sql)

        findings: List[Dict[str, Any]] = []
        for (tid, fp), items in grouped.items():
            if len(items) < min_count:
                continue
            total_ms = sum(float(i.get("duration_ms") or 0.0) for i in items)
            findings.append({
                "fingerprint": fp,
                "count": len(items),
                "total_duration_ms": round(total_ms, 3),
                "sample_query": samples[(tid, fp)],
                "trace_id": tid,
            })

        findings.sort(key=lambda item: (-item["count"], -item["total_duration_ms"], item["fingerprint"]))
        return findings

    def _operation_key(self, event: Dict[str, Any]) -> Optional[Tuple[str, str]]:
        etype = event.get("event_type", "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}

        if etype in _HTTP_EVENT_TYPES:
            method = str(payload.get("method", "HTTP")).upper()
            target = str(payload.get("path") or payload.get("url") or "/")
            return ("HTTP", f"{method} {target}")

        if etype in _SQL_EVENT_TYPES:
            raw_sql = self._extract_sql_text(event)
            if not raw_sql:
                return None
            return ("SQL", self.normalize_sql_fingerprint(raw_sql))

        if etype in _SUBPROCESS_EVENT_TYPES:
            cmd = payload.get("command") or payload.get("args") or ""
            if isinstance(cmd, list):
                cmd_str = " ".join(str(c) for c in cmd)
            else:
                cmd_str = str(cmd)
            cmd_str = " ".join(cmd_str.split())
            if not cmd_str:
                return None
            return ("SUBPROCESS", cmd_str)

        return None

    def detect_latency_regressions(
        self,
        wal_path: str,
        baseline_path: str = ".blackbox/wal.baseline.sqlite",
        min_delta_ms: float = 50.0,
        factor: float = 3.0,
    ) -> List[Dict[str, Any]]:
        """
        Compares homologous HTTP, SQL, and SUBPROCESS event durations in wal_path
        against baseline_path, reporting operations exceeding factor * baseline_ms
        by at least min_delta_ms.
        """
        if not wal_path or not baseline_path:
            return []
        if not os.path.isfile(wal_path) or not os.path.isfile(baseline_path):
            return []

        b_ring = WalRingBuffer(db_path=baseline_path)
        try:
            b_events = b_ring.get_timeline(limit=5000)
        finally:
            b_ring.close()

        w_ring = WalRingBuffer(db_path=wal_path)
        try:
            w_events = w_ring.get_timeline(limit=5000)
        finally:
            w_ring.close()

        baseline_durations: Dict[Tuple[str, str], List[float]] = {}
        for ev in b_events:
            dur = ev.get("duration_ms")
            if dur is None:
                continue
            op = self._operation_key(ev)
            if op is None:
                continue
            baseline_durations.setdefault(op, []).append(float(dur))

        current_latest: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for ev in w_events:
            dur = ev.get("duration_ms")
            if dur is None:
                continue
            op = self._operation_key(ev)
            if op is None or op not in baseline_durations:
                continue
            prev = current_latest.get(op)
            if prev is None or float(dur) >= float(prev.get("duration_ms") or 0.0):
                current_latest[op] = ev

        regressions: List[Dict[str, Any]] = []
        for op, ev in current_latest.items():
            base_ms = float(median(baseline_durations[op]))
            curr_ms = float(ev.get("duration_ms") or 0.0)
            delta_ms = curr_ms - base_ms
            if delta_ms >= float(min_delta_ms) and (base_ms <= 0.0 or curr_ms >= base_ms * float(factor)):
                category, op_name = op
                regressions.append({
                    "category": category,
                    "operation": op_name,
                    "baseline_ms": round(base_ms, 3),
                    "current_ms": round(curr_ms, 3),
                    "delta_ms": round(delta_ms, 3),
                    "factor": round(curr_ms / base_ms, 2) if base_ms > 0 else None,
                    "event_id": ev.get("id"),
                    "trace_id": self._extract_trace_id(ev),
                })

        regressions.sort(key=lambda r: (-r["delta_ms"], r["operation"]))
        return regressions

