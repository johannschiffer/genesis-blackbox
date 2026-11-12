"""
Genesis Blackbox: Flaky Test & Non-Determinism Analyzer
Inspects execution run histories in WAL and baseline snapshots to detect status
alternation without source changes, non-deterministic divergence signals, N+1 SQL
patterns, and latency regressions.
"""

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from ..recorder.wal_ring import WalRingBuffer
from .perf_detector import PerformanceDetector
from .state_differ import StateDiffer

_UUID_PATTERN = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_ISO_TS_PATTERN = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")

_NETWORK_KEYWORDS = (
    "timeouterror", "timed out", "timeout", "connectionerror", "connectionrefused",
    "econnrefused", "econnreset", "etimedout", "502", "503", "504", "network",
)
_PORT_LOCK_KEYWORDS = (
    "address already in use", "eaddrinuse", "database is locked",
    "resource temporarily unavailable", "port",
)
_TIMESTAMP_UUID_KEYWORDS = ("timestamp", "datetime", "uuid", "nonce", "random", "created_at", "updated_at")


class FlakeAnalyzer:
    """Correlates run status oscillation, non-deterministic causes, and performance anomalies."""

    @staticmethod
    def _run_target_key(run: Dict[str, Any]) -> str:
        for ev in run.get("events", []):
            etype = ev.get("event_type", "")
            payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
            if etype == "TEST_START" and payload.get("nodeid"):
                return f"test:{payload['nodeid']}"
            if etype == "PROCESS_START" and payload.get("script"):
                return f"script:{payload['script']}"
            if etype == "SUBPROCESS_START" and payload.get("command"):
                cmd = payload["command"]
                return "cmd:" + (" ".join(str(c) for c in cmd) if isinstance(cmd, list) else str(cmd))
        return "default"

    @staticmethod
    def _extract_source_hashes(run: Dict[str, Any]) -> Optional[str]:
        for ev in run.get("events", []):
            payload = ev.get("payload")
            if isinstance(payload, dict) and payload.get("source_hash"):
                return str(payload["source_hash"])
        return None

    def _detect_non_determinism_causes(
        self,
        runs: List[Dict[str, Any]],
        baseline_exists: bool,
        differ: StateDiffer,
    ) -> Tuple[List[str], int]:
        """Inspects exceptions and run divergence for common sources of flakiness."""
        causes: List[str] = []
        status_flips = 0

        # 1. Check status alternation across runs for the same target (or overall)
        by_target: Dict[str, List[Dict[str, Any]]] = {}
        for r in runs:
            by_target.setdefault(self._run_target_key(r), []).append(r)

        same_hash_flip = False
        for target_runs in by_target.values():
            prev_status = None
            prev_hash = None
            for r in target_runs:
                st = r.get("status")
                sh = self._extract_source_hashes(r)
                if prev_status is not None and st != prev_status:
                    status_flips += 1
                    if sh is None or prev_hash is None or sh == prev_hash:
                        same_hash_flip = True
                prev_status = st
                prev_hash = sh

        if status_flips >= 1 and same_hash_flip:
            causes.append(
                f"Run status oscillated between passing and failing ({status_flips} transition(s)) without source hash changes"
            )

        # 2. Check exception messages and payloads across failing runs
        blob_parts: List[str] = []
        for r in runs:
            for ev in r.get("events", []):
                if ev.get("event_type") == "EXCEPTION" or ev.get("status") == "error":
                    blob_parts.append(str(ev.get("payload", "")))
        combined_err = " ".join(blob_parts).lower()

        if any(k in combined_err for k in _PORT_LOCK_KEYWORDS):
            causes.append("Port contention or lock collision detected (address/port in use or SQLite lock)")
        if any(k in combined_err for k in _NETWORK_KEYWORDS):
            causes.append("Transient network or timeout failure detected")
        if _UUID_PATTERN.search(combined_err) or _ISO_TS_PATTERN.search(combined_err) or any(
            k in combined_err for k in _TIMESTAMP_UUID_KEYWORDS
        ):
            causes.append("Unfixed timestamp, clock, or UUID/random identifier referenced in failure context")

        # 3. Check structural divergence if passing & failing runs exist
        has_pass = any(r.get("status") == "passing" for r in runs)
        has_fail = any(r.get("status") == "failing" for r in runs)
        if has_pass and has_fail:
            try:
                diff_res = differ.diff()
                alignments = diff_res.get("alignment_diff", [])
                seq_shifts = [a for a in alignments if "Event type shift" in str(a.get("diff_note", ""))]
                if seq_shifts:
                    causes.append("Execution event ordering shifted between passing and failing runs (collection/async race)")
                div_desc = str((diff_res.get("divergence_point") or {}).get("description", ""))
                if _UUID_PATTERN.search(div_desc) or _ISO_TS_PATTERN.search(div_desc):
                    msg = "Non-deterministic timestamp or UUID divergence detected between runs"
                    if msg not in causes:
                        causes.append(msg)
            except Exception:
                pass
        elif baseline_exists and has_fail and status_flips == 0:
            # Cross-check against baseline if WAL only has failing run
            pass

        # Deduplicate while preserving order
        deduped = list(dict.fromkeys(causes))
        return deduped, status_flips

    def analyze_flakiness(
        self,
        wal_path: str = ".blackbox/wal.sqlite",
        baseline_path: str = ".blackbox/wal.baseline.sqlite",
        n_plus_one_threshold: int = 5,
    ) -> Dict[str, Any]:
        """
        Evaluates WAL run oscillation, non-determinism causes, N+1 SQL queries,
        and latency regressions against baseline_path.
        """
        perf = PerformanceDetector()
        n_plus_one: List[Dict[str, Any]] = []
        latency_regressions: List[Dict[str, Any]] = []
        suspect_causes: List[str] = []
        status_flips = 0

        if wal_path and os.path.isfile(wal_path):
            ring = WalRingBuffer(db_path=wal_path)
            try:
                timeline = ring.get_timeline(limit=5000)
                n_plus_one = perf.detect_n_plus_one(timeline, threshold=n_plus_one_threshold)
                differ = StateDiffer(wal_ring=ring)
                runs = differ.extract_runs()
                baseline_exists = bool(baseline_path and os.path.isfile(baseline_path))
                suspect_causes, status_flips = self._detect_non_determinism_causes(
                    runs=runs,
                    baseline_exists=baseline_exists,
                    differ=differ,
                )
            finally:
                ring.close()

            if baseline_path and os.path.isfile(baseline_path):
                latency_regressions = perf.detect_latency_regressions(
                    wal_path=wal_path,
                    baseline_path=baseline_path,
                )

        if latency_regressions and not any("timeout" in c.lower() for c in suspect_causes):
            suspect_causes.append(
                f"Severe latency regression ({len(latency_regressions)} operation(s)) may trigger timing/timeout flakiness"
            )

        confidence = 0.0
        if status_flips >= 2:
            confidence = 0.9
        elif status_flips == 1:
            confidence = 0.65
        if suspect_causes:
            confidence = min(0.98, max(confidence, 0.35) + 0.15 * min(len(suspect_causes), 3))

        is_flaky = bool(status_flips >= 1 or (confidence >= 0.5 and len(suspect_causes) >= 1))

        md: List[str] = [
            "# BLACKBOX PERFORMANCE & FLAKINESS DIAGNOSTICS",
            f"- **Flaky Classification:** `{'FLAKY' if is_flaky else 'DETERMINISTIC'}` (Confidence: `{confidence:.2f}`)",
            f"- **N+1 SQL Patterns:** `{len(n_plus_one)}`",
            f"- **Latency Regressions:** `{len(latency_regressions)}`",
            "",
        ]
        if suspect_causes:
            md.append("## Suspect Flakiness Causes")
            for cause in suspect_causes:
                md.append(f"- {cause}")
            md.append("")
        if n_plus_one:
            md.append("## N+1 SQL Query Alerts")
            for item in n_plus_one:
                md.append(
                    f"- `{item['fingerprint']}` executed **{item['count']}x** "
                    f"(total `{item['total_duration_ms']:.2f}ms`, trace=`{item.get('trace_id')}`)"
                )
            md.append("")
        if latency_regressions:
            md.append("## Latency Regressions vs Baseline")
            for reg in latency_regressions:
                md.append(
                    f"- `[{reg['category']}] {reg['operation']}`: "
                    f"`{reg['baseline_ms']:.2f}ms` -> `{reg['current_ms']:.2f}ms` "
                    f"(+{reg['delta_ms']:.2f}ms, {reg.get('factor')}x)"
                )
            md.append("")

        return {
            "is_flaky": is_flaky,
            "confidence": round(confidence, 2),
            "suspect_causes": suspect_causes,
            "n_plus_one_queries": n_plus_one,
            "latency_regressions": latency_regressions,
            "markdown_report": "\n".join(md),
        }

