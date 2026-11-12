"""
Genesis Blackbox: Autonomous Heal Planner
Synthesizes WAL crash telemetry, baseline divergence, offline HTTP egress stubs,
performance/resource alerts, AST blast radius, and impacted regression tests into a
deterministic 4-step repair plan (JSON and Markdown).
"""

import os
from typing import Any, Dict, List, Optional

from ..ast_engine.graph import CodebaseGraph
from ..ast_engine.impact import ImpactTestSelector
from ..recorder.egress_mocker import EgressMocker
from ..recorder.wal_ring import WalRingBuffer
from .perf_detector import PerformanceDetector
from .reproducer import CrashReproducer
from .state_differ import StateDiffer


class AutonomousHealPlanner:
    """Builds a deterministic step-by-step remediation plan from WAL incident telemetry."""

    def build_heal_plan(
        self,
        wal_path: str = ".blackbox/wal.sqlite",
        baseline_path: str = ".blackbox/wal.baseline.sqlite",
        root_dir: str = ".",
        n_plus_one_threshold: int = 5,
    ) -> Dict[str, Any]:
        abs_root = os.path.abspath(root_dir)
        graph = CodebaseGraph()

        has_crash = False
        root_cause_info: Dict[str, Any] = {}
        egress_stubs: List[Dict[str, Any]] = []
        resource_profile: Optional[Dict[str, Any]] = None
        perf_alerts: Dict[str, Any] = {
            "n_plus_one_queries": [],
            "latency_regressions": [],
        }
        baseline_divergence: Optional[Dict[str, Any]] = None
        affected_files: List[str] = []
        blast_radius: Dict[str, Any] = {}
        reproduction_test_file = "test_blackbox_reproduce.py"

        with WalRingBuffer(db_path=wal_path) as ring:
            exc_events = ring.query_recent(limit=5, event_type="EXCEPTION")
            recent_events = ring.query_recent(limit=200)
            crash_trace_id: Optional[str] = None
            target_fn: Optional[str] = None
            culprit_file: Optional[str] = None

            if exc_events:
                has_crash = True
                latest_exc = exc_events[0]
                exc_payload = latest_exc.get("payload") or {}
                rc = exc_payload.get("root_cause") or {}
                frames = exc_payload.get("frames") or []

                crash_trace_id = (
                    latest_exc.get("trace_id")
                    or exc_payload.get("trace_id")
                    or rc.get("trace_id")
                )
                culprit_file = rc.get("filename")
                target_fn = rc.get("function")

                root_cause_info = {
                    "exception_type": exc_payload.get("exception_type", "UnknownException"),
                    "exception_message": exc_payload.get("exception_message", ""),
                    "filename": culprit_file or "",
                    "function": target_fn or "",
                    "lineno": rc.get("lineno"),
                    "trace_id": crash_trace_id,
                }

                for candidate in [culprit_file] + [f.get("filename") for f in frames if isinstance(f, dict)]:
                    if candidate and os.path.isfile(candidate):
                        norm = os.path.normpath(os.path.abspath(candidate))
                        if norm not in affected_files:
                            affected_files.append(norm)

            egress_stubs = EgressMocker.extract_egress_stubs(
                recent_events,
                trace_id=crash_trace_id,
            )

            for ev in recent_events:
                if ev.get("event_type") != "RESOURCE_PROFILE":
                    continue
                ev_trace = ev.get("trace_id") or (
                    ev.get("payload").get("trace_id") if isinstance(ev.get("payload"), dict) else None
                )
                if crash_trace_id and ev_trace and ev_trace != crash_trace_id:
                    continue
                if isinstance(ev.get("payload"), dict):
                    resource_profile = dict(ev["payload"])
                    break

            scoped_events = (
                [
                    e for e in recent_events
                    if (e.get("trace_id") == crash_trace_id)
                    or (isinstance(e.get("payload"), dict) and e["payload"].get("trace_id") == crash_trace_id)
                ]
                if crash_trace_id
                else recent_events
            )
            detector = PerformanceDetector()
            perf_alerts["n_plus_one_queries"] = detector.detect_n_plus_one(
                scoped_events or recent_events,
                threshold=n_plus_one_threshold,
            )
            if baseline_path and os.path.isfile(baseline_path):
                perf_alerts["latency_regressions"] = detector.detect_latency_regressions(
                    wal_path=wal_path,
                    baseline_path=baseline_path,
                )
                try:
                    differ = StateDiffer(wal_ring=ring)
                    baseline_divergence = differ.diff_against_baseline(baseline_path)
                except Exception:
                    baseline_divergence = None

            reproducer = CrashReproducer(wal_ring=ring)
            reproduction_test_file = reproducer.get_recommended_filename()

        if culprit_file and os.path.isfile(culprit_file):
            if os.path.isdir(abs_root):
                graph.parse_directory(abs_root)
            else:
                graph.parse_file(culprit_file)
            if target_fn:
                blast_radius = graph.get_blast_radius(
                    target_function=target_fn,
                    file_path=culprit_file,
                )
                for sym in blast_radius.get("upstream_callers", []) + blast_radius.get("downstream_callees", []):
                    node_file = (graph.nodes.get(sym) or {}).get("filename")
                    if node_file and os.path.isfile(node_file):
                        norm = os.path.normpath(os.path.abspath(node_file))
                        if norm not in affected_files:
                            affected_files.append(norm)

        selector = ImpactTestSelector(root_dir=abs_root, graph=graph)
        impacted_tests = selector.find_impacted_tests(
            files=affected_files if affected_files else None,
            diff=True,
        )

        steps = self._build_steps(
            has_crash=has_crash,
            root_cause=root_cause_info,
            egress_stubs=egress_stubs,
            perf_alerts=perf_alerts,
            resource_profile=resource_profile,
            baseline_divergence=baseline_divergence,
            affected_files=affected_files,
            impacted_tests=impacted_tests,
            reproduction_test_file=reproduction_test_file,
            wal_path=wal_path,
            baseline_path=baseline_path,
        )

        plan: Dict[str, Any] = {
            "has_crash": has_crash,
            "root_cause": root_cause_info,
            "egress_stubs": egress_stubs,
            "resource_profile": resource_profile,
            "performance_alerts": perf_alerts,
            "baseline_divergence": baseline_divergence,
            "blast_radius": blast_radius,
            "affected_files": affected_files,
            "impacted_tests": impacted_tests,
            "reproduction_test_file": reproduction_test_file,
            "steps": steps,
        }
        plan["markdown_plan"] = self._render_markdown(plan)
        return plan

    @staticmethod
    def _build_steps(
        has_crash: bool,
        root_cause: Dict[str, Any],
        egress_stubs: List[Dict[str, Any]],
        perf_alerts: Dict[str, Any],
        resource_profile: Optional[Dict[str, Any]],
        baseline_divergence: Optional[Dict[str, Any]],
        affected_files: List[str],
        impacted_tests: List[str],
        reproduction_test_file: str,
        wal_path: str,
        baseline_path: str,
    ) -> List[Dict[str, Any]]:
        culprit_ref = (
            f"{root_cause.get('filename')}:{root_cause.get('lineno')}"
            if root_cause.get("filename")
            else "WAL target"
        )
        step1_cmd = (
            f"blackbox reproduce --db {wal_path} --output {reproduction_test_file} && pytest -v {reproduction_test_file}"
            if reproduction_test_file.endswith(".py")
            else f"blackbox reproduce --db {wal_path} --output {reproduction_test_file} && node --test {reproduction_test_file}"
        )
        return [
            {
                "step": 1,
                "phase": "reproduce_offline",
                "title": "Synthesize & Execute Offline Reproduction Test",
                "description": (
                    f"Generate `{reproduction_test_file}` with {len(egress_stubs)} embedded offline HTTP egress stub(s) "
                    "and confirm deterministic failure before editing source code."
                    if has_crash
                    else "Verify clean baseline execution or synthesize target regression test."
                ),
                "commands": [step1_cmd],
                "targets": [reproduction_test_file],
            },
            {
                "step": 2,
                "phase": "inspect_frames_and_diff",
                "title": "Inspect Culprit Frames, Baseline Divergence & Resource Alerts",
                "description": (
                    f"Inspect `{culprit_ref}` alongside baseline divergence "
                    f"({'present' if baseline_divergence else 'none'}), "
                    f"{len(perf_alerts.get('n_plus_one_queries', []))} N+1 SQL finding(s), and "
                    f"peak memory `{resource_profile.get('peak_kb', 0) if resource_profile else 0} KiB`."
                ),
                "commands": [
                    f"blackbox compile --db {wal_path}",
                    f"blackbox diagnose-perf --db {wal_path} --baseline {baseline_path}",
                ],
                "targets": list(affected_files),
            },
            {
                "step": 3,
                "phase": "validate_candidate_patch",
                "title": "Validate Candidate Patch in Ephemeral Sandbox",
                "description": (
                    "Apply minimal fix in affected file(s) and dry-run candidate unified diff via "
                    "PatchValidator (`test_candidate_patch`) with rollback guarantee."
                ),
                "commands": ["MCP tool: test_candidate_patch(patch_diff, test_path)"],
                "targets": list(affected_files),
            },
            {
                "step": 4,
                "phase": "verify_and_emit_receipt",
                "title": "Verify Causal Fix & Run AST-Impacted Regression Suite",
                "description": (
                    f"Execute `{reproduction_test_file}` and {len(impacted_tests)} AST-impacted test(s), "
                    "emitting `.blackbox-resolved.md` and rotating clean baseline."
                ),
                "commands": [
                    f"blackbox verify --test {reproduction_test_file} --db {wal_path}",
                    "blackbox test-impact --run",
                ],
                "targets": [reproduction_test_file] + list(impacted_tests),
            },
        ]

    @staticmethod
    def _render_markdown(plan: Dict[str, Any]) -> str:
        rc = plan.get("root_cause") or {}
        lines = [
            "# Genesis Blackbox Autonomous Heal Plan",
            "",
            f"- **Incident Status:** `{'CRASH DETECTED' if plan.get('has_crash') else 'NO ACTIVE CRASH'}`",
        ]
        if plan.get("has_crash"):
            lines.append(
                f"- **Root Cause:** `{rc.get('exception_type')}`: {rc.get('exception_message')} "
                f"(`{rc.get('filename')}:{rc.get('lineno')}` in `{rc.get('function')}()`)"
            )
            if rc.get("trace_id"):
                lines.append(f"- **Trace ID:** `{rc.get('trace_id')}`")

        lines.extend([
            f"- **Offline HTTP Egress Stubs:** `{len(plan.get('egress_stubs', []))}`",
            f"- **Affected Files:** {', '.join(f'`{f}`' for f in plan.get('affected_files', [])) or 'None'}",
            f"- **AST-Impacted Tests:** {', '.join(f'`{t}`' for t in plan.get('impacted_tests', [])) or 'None'}",
            f"- **N+1 SQL Alerts:** `{len(plan.get('performance_alerts', {}).get('n_plus_one_queries', []))}`",
            f"- **Latency Regressions:** `{len(plan.get('performance_alerts', {}).get('latency_regressions', []))}`",
        ])

        rp = plan.get("resource_profile")
        if isinstance(rp, dict):
            lines.append(
                f"- **Resource Footprint:** peak=`{rp.get('peak_kb', 0)} KiB`, "
                f"delta=`{rp.get('delta_kb', 0)} KiB`, open_fds=`{rp.get('open_fds')}`"
            )

        lines.extend(["", "## Deterministic Remediation Steps"])
        for step in plan.get("steps", []):
            lines.append(f"### Step {step['step']}: {step['title']} (`{step['phase']}`)")
            lines.append(step["description"])
            for cmd in step.get("commands", []):
                lines.append(f"- `{cmd}`")
            lines.append("")

        return "\n".join(lines)
