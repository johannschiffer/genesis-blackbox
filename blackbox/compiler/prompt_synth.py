"""
Genesis Blackbox: Autonomous AI Agent Repair Prompt Synthesizer
Compiles deterministic repair packets combining causal autopsy, standalone reproduction tests,
polyglot AST blast-radius analysis, and time-travel execution diffs for AI coding agents.
"""

import os
import sys
import json
from typing import Dict, Any, List, Optional

from ..recorder.wal_ring import WalRingBuffer
from ..ast_engine.graph import CodebaseGraph
from ..ast_engine.polyglot import PolyglotParser
from ..ast_engine.impact import ImpactTestSelector
from .autopsy import CausalCrashCompiler
from .reproducer import CrashReproducer
from .state_differ import StateDiffer


_SKIP_DIRS = frozenset({
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", "dist", "build"
})


class PromptSynthesizer:
    """
    Synthesizes complete, context-dense repair prompt packets for AI coding agents.
    Packages crash autopsy, polyglot reproduction tests, AST blast-radius, and execution divergence.
    """

    def __init__(
        self,
        wal_ring: Optional[WalRingBuffer] = None,
        ast_graph: Optional[CodebaseGraph] = None,
        repo_root: str = ".",
        db_path: str = ".blackbox/wal.sqlite"
    ):
        self.repo_root = os.path.abspath(repo_root)
        self.db_path = db_path
        self._owns_ring = wal_ring is None
        self.wal_ring = wal_ring or WalRingBuffer(db_path=self.db_path)
        self.ast_graph = ast_graph or CodebaseGraph()
        self.impact_selector = ImpactTestSelector(root_dir=self.repo_root, graph=self.ast_graph)

    def close(self) -> None:
        """Closes the WAL ring only if this synthesizer opened it."""
        if self._owns_ring:
            self.wal_ring.close()

    def __enter__(self) -> "PromptSynthesizer":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def synthesize(self) -> str:
        """Generates full markdown repair prompt packet for the latest crash in WAL."""
        excs = self.wal_ring.query_recent(limit=1, event_type="EXCEPTION")
        if not excs:
            return (
                "# Genesis Blackbox Repair Prompt Packet\n\n"
                "> **Status:** No crash or uncaught exception found in flight recorder WAL database.\n"
                "> Ensure your command was run with `blackbox record -- <cmd>`.\n"
            )

        exc_event = excs[0]
        payload = exc_event.get("payload", {})
        runtime = payload.get("runtime", "")
        exc_type = payload.get("exception_type", "Error")
        exc_msg = payload.get("exception_message", "")
        root_cause = payload.get("root_cause", {})
        filename = root_cause.get("filename", "")
        lineno = root_cause.get("lineno", 0)
        func_name = root_cause.get("function", "")

        is_node = (runtime == "node") or any(
            filename.endswith(ext)
            for ext in [".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"]
        )

        if filename.endswith((".ts", ".tsx")):
            lang_fence = "typescript"
            lang_name = "TypeScript"
            repro_name = "test_blackbox_reproduce.mjs"
        elif is_node:
            lang_fence = "javascript"
            lang_name = "JavaScript (Node.js)"
            repro_name = "test_blackbox_reproduce.mjs"
        else:
            lang_fence = "python"
            lang_name = "Python"
            repro_name = "test_blackbox_reproduce.py"

        # 1. Autopsy Capsule
        compiler = CausalCrashCompiler(wal_ring=self.wal_ring, ast_graph=self.ast_graph)
        autopsy_md = compiler.compile()

        # 2. Reproduction Test Script
        reproducer = CrashReproducer(wal_ring=self.wal_ring)
        repro_code = reproducer.generate()

        # 3. Blast Radius and Dependency Impact
        blast_radius_info = self._resolve_blast_radius(filename, func_name, is_node)

        # 4. Time-Travel Diff
        diff_md = self._resolve_time_travel_diff()

        # Assemble Markdown Packet
        lines = [
            f"# 🛠️ BLACKBOX REPAIR PACKET: {exc_type}",
            "",
            "> **Autonomous AI Coding Agent Mandate:**",
            f"> An uncaught runtime crash occurred in {lang_name}. Analyze the causal autopsy,",
            f"> inspect the standalone reproduction test, assess the blast radius, and apply a surgical fix.",
            "",
            "## 1. Executive Summary & Culprit Location",
            "",
            f"- **Runtime:** `{lang_name}`",
            f"- **Exception Type:** `{exc_type}`",
            f"- **Error Message:** `{exc_msg}`",
            f"- **Culprit File:** `{filename}`",
            f"- **Culprit Function:** `{func_name or '<module>'}` (Line {lineno})",
            "",
            "## 2. Blast-Radius & Dependency Impact",
            "",
            blast_radius_info,
            "",
            f"## 3. Standalone Reproduction Test (`{repro_name}`)",
            "",
            f"Run this reproduction test to observe failure before repair and verify resolution afterward:",
            f"```{lang_fence}",
            repro_code.strip(),
            "```",
            "",
            "## 4. Flight-Recorder Causal Autopsy",
            "",
            autopsy_md.strip(),
            ""
        ]

        if diff_md:
            lines.extend([
                "## 5. Time-Travel Execution Diff (Passing vs. Failing Run)",
                "",
                diff_md.strip(),
                ""
            ])

        lines.extend([
            "## 6. Repair Verification Mandate",
            "",
            "1. **Analyze:** Inspect the culprit line and local variables recorded in the autopsy above.",
            "2. **Patch:** Apply the minimal required fix in the culprit source file.",
            "3. **Verify:**",
            f"   - Run the reproduction test: `{('node --test ' + repro_name) if is_node else ('pytest ' + repro_name)}`",
            "   - Validate resolution receipt: `blackbox verify`",
            "   - Check regression impact: `blackbox test-impact`",
            "4. **No Regressions:** Ensure existing caller dependencies within the blast radius remain intact.",
            ""
        ])

        return "\n".join(lines)

    generate = synthesize

    def _resolve_blast_radius(self, filename: str, func_name: str, is_node: bool) -> str:
        """Resolves AST blast radius for culprit file and function."""
        if not filename or not os.path.exists(filename):
            return "> Blast radius unavailable: Culprit source file not accessible on disk."

        upstream_callers = []
        downstream_callees = []

        try:
            if is_node:
                # Use PolyglotParser
                with open(filename, "r", encoding="utf-8", errors="replace") as f:
                    p_res = PolyglotParser(file_path=filename).parse(f.read())
                # V8 names methods Class.method; the parser indexes the bare name.
                short_name = func_name.split(".")[-1] if func_name else ""
                func_data = p_res["functions"].get(func_name) or p_res["functions"].get(short_name) or {}
                downstream_callees = list(func_data.get("calls", []))

                if short_name:
                    for fn_name, fn_info in p_res["functions"].items():
                        if fn_name != short_name and short_name in fn_info.get("calls", []):
                            upstream_callers.append(f"{os.path.basename(filename)}:{fn_name}()")

                # Search codebase for callers
                for root, dirs, files in os.walk(self.repo_root):
                    # Prune in place so os.walk never descends into vendored or build trees.
                    dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
                    for fl in files:
                        if fl.endswith((".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")):
                            fpath = os.path.join(root, fl)
                            if os.path.normpath(fpath) == os.path.normpath(os.path.abspath(filename)):
                                continue
                            try:
                                with open(fpath, "r", encoding="utf-8", errors="replace") as f_other:
                                    other_res = PolyglotParser(file_path=fpath).parse(f_other.read())
                                for fn_name, fn_info in other_res["functions"].items():
                                    if short_name and short_name in fn_info.get("calls", []):
                                        upstream_callers.append(f"{fl}:{fn_name}()")
                            except Exception:
                                pass
            else:
                # A single-file graph cannot see callers in other modules.
                if len(self.ast_graph.nodes) <= 1:
                    self.ast_graph.parse_directory(self.repo_root, max_files=100)
                self.ast_graph.parse_file(filename)
                if func_name:
                    radius = self.ast_graph.get_blast_radius(func_name, file_path=filename)
                    upstream_callers = radius.get("upstream_callers", [])
                    downstream_callees = radius.get("downstream_callees", [])
        except Exception:
            pass

        impacted_tests = []
        try:
            impacted_tests = self.impact_selector.find_impacted_tests(files=[filename], diff=False)
        except Exception:
            pass

        out = []
        out.append(f"- **Target Symbol:** `{func_name or os.path.basename(filename)}`")
        out.append(f"- **Upstream Callers ({len(upstream_callers)}):** {', '.join(f'`{c}`' for c in upstream_callers) if upstream_callers else '_None detected_'}")
        out.append(f"- **Downstream Callees ({len(downstream_callees)}):** {', '.join(f'`{c}`' for c in downstream_callees) if downstream_callees else '_None detected_'}")
        out.append(f"- **Impacted Test Suites ({len(impacted_tests)}):** {', '.join(f'`{os.path.basename(t)}`' for t in impacted_tests) if impacted_tests else '_No impacted test suites detected_'}")

        return "\n".join(out)

    def _resolve_time_travel_diff(self) -> Optional[str]:
        """Resolves differential execution analysis if multiple runs exist in WAL."""
        try:
            differ = StateDiffer(wal_ring=self.wal_ring)
            diff_res = differ.diff()
            if diff_res and diff_res.get("divergence_point"):
                return differ.compile_markdown(diff_res)
        except Exception:
            pass
        return None
