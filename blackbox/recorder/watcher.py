"""
Genesis Blackbox: Surgical Watch Mode
Zero-dependency stdlib file watcher using os.stat polling.
Detects source modifications, resolves impacted tests via AST ImpactTestSelector,
runs only impacted tests with Blackbox capture, rotates baseline on clean pass,
and compiles autopsy/diff/reproducer artifacts immediately on failure.
"""

import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from ..ast_engine.graph import JS_TS_EXTENSIONS
from ..ast_engine.impact import ImpactTestSelector
from ..compiler.autopsy import CausalCrashCompiler
from ..compiler.reproducer import CrashReproducer
from .baseline import BaselineManager
from .runner import run_target
from .wal_ring import WalRingBuffer

WATCHED_EXTENSIONS = (".py", ".ts", ".tsx", ".js", ".mjs")
IGNORED_DIRS = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__", ".blackbox", ".pytest_cache"})
GENERATED_ARTIFACTS = frozenset({"test_blackbox_reproduce.py", "test_blackbox_reproduce.mjs"})


class SurgicalWatcher:
    """Polls source file mtimes via os.stat and executes surgical impacted-test runs."""

    def __init__(
        self,
        root_dir: str = ".",
        db_path: str = ".blackbox/wal.sqlite",
        baseline_path: Optional[str] = None,
        interval: float = 1.0,
        auto_baseline: bool = True,
    ):
        self.root_dir = os.path.abspath(root_dir)
        self.db_path = db_path if os.path.isabs(db_path) else os.path.join(self.root_dir, db_path)
        self.baseline_path = baseline_path or BaselineManager.default_baseline_path(self.db_path)
        self.interval = max(0.05, float(interval))
        self.auto_baseline = auto_baseline
        self._snapshot: Dict[str, Tuple[int, int]] = self._scan_files()

    def _scan_files(self) -> Dict[str, Tuple[int, int]]:
        """Collects (st_mtime_ns, st_size) via os.stat for all watched source files."""
        snapshot: Dict[str, Tuple[int, int]] = {}
        for current_root, dirs, files in os.walk(self.root_dir):
            dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
            for fname in files:
                if fname in GENERATED_ARTIFACTS or not fname.endswith(WATCHED_EXTENSIONS):
                    continue
                fpath = os.path.normpath(os.path.join(current_root, fname))
                try:
                    st = os.stat(fpath)
                    snapshot[fpath] = (st.st_mtime_ns, st.st_size)
                except OSError:
                    continue
        return snapshot

    def _detect_changes(self) -> List[str]:
        """Compares current os.stat snapshot against previous state and updates snapshot."""
        current = self._scan_files()
        changed: List[str] = []
        for fpath, sig in current.items():
            if fpath not in self._snapshot or self._snapshot[fpath] != sig:
                changed.append(fpath)
        self._snapshot = current
        return sorted(changed)

    def _execute_impacted_tests(self, impacted_tests: List[str], autopsy_path: str) -> Tuple[int, str]:
        """Runs impacted tests with Blackbox flight recording attached."""
        py_tests = [t for t in impacted_tests if t.endswith(".py")]
        js_tests = [t for t in impacted_tests if t.endswith(JS_TS_EXTENSIONS)]

        outputs: List[str] = []
        exit_code = 0

        if py_tests:
            cmd = [
                sys.executable,
                "-m",
                "pytest",
                *py_tests,
                "-p",
                "blackbox.integrations.pytest_plugin",
                "--blackbox",
                f"--blackbox-db={self.db_path}",
                f"--blackbox-output={autopsy_path}",
                f"--blackbox-baseline={self.baseline_path}",
                "-v",
            ]
            if not self.auto_baseline:
                cmd.append("--no-auto-baseline")
            proc = subprocess.run(cmd, cwd=self.root_dir, capture_output=True, text=True)
            if proc.stdout:
                outputs.append(proc.stdout)
            if proc.stderr:
                outputs.append(proc.stderr)
            if proc.returncode != 0:
                exit_code = proc.returncode

        if js_tests:
            rc_js = run_target(
                ["node", "--test", *js_tests],
                db_path=self.db_path,
                auto_baseline=self.auto_baseline,
                baseline_path=self.baseline_path,
                autopsy_path=autopsy_path,
            )
            if rc_js != 0 and exit_code == 0:
                exit_code = rc_js

        return exit_code, "\n".join(outputs).strip()

    def _compile_failure_artifacts(self, autopsy_path: str) -> Dict[str, Optional[str]]:
        """Ensures .blackbox.md, .blackbox-diff.md (when baseline exists), and reproducer exist."""
        diff_path = os.path.join(self.root_dir, ".blackbox-diff.md")
        reproducer_path: Optional[str] = None

        with WalRingBuffer(db_path=self.db_path) as ring:
            if not os.path.isfile(autopsy_path):
                compiler = CausalCrashCompiler(wal_ring=ring)
                capsule = compiler.compile()
                os.makedirs(os.path.dirname(os.path.abspath(autopsy_path)) or ".", exist_ok=True)
                with open(autopsy_path, "w", encoding="utf-8") as f_aut:
                    f_aut.write(capsule)

            reproducer = CrashReproducer(wal_ring=ring)
            rec_name = reproducer.get_recommended_filename()
            reproducer_path = os.path.join(self.root_dir, rec_name)
            if not os.path.isfile(reproducer_path):
                code = reproducer.generate()
                with open(reproducer_path, "w", encoding="utf-8") as f_rep:
                    f_rep.write(code)

        if os.path.isfile(self.baseline_path):
            BaselineManager.write_baseline_divergence(
                wal_path=self.db_path,
                baseline_path=self.baseline_path,
                autopsy_path=autopsy_path,
                diff_output_path=diff_path,
            )

        return {
            "autopsy_path": autopsy_path if os.path.isfile(autopsy_path) else None,
            "diff_path": diff_path if os.path.isfile(diff_path) else None,
            "reproducer_path": reproducer_path if (reproducer_path and os.path.isfile(reproducer_path)) else None,
        }

    def run_once(self) -> Dict[str, Any]:
        """Performs one deterministic poll-and-verify cycle."""
        changed_files = self._detect_changes()
        if not changed_files:
            return {
                "changed_files": [],
                "impacted_tests": [],
                "executed": False,
                "passed": True,
                "exit_code": 0,
                "baseline_promoted": False,
                "autopsy_path": None,
                "diff_path": None,
                "reproducer_path": None,
                "output": "",
            }

        selector = ImpactTestSelector(self.root_dir)
        impacted_tests = selector.select_tests_for_files(changed_files)
        if not impacted_tests:
            return {
                "changed_files": changed_files,
                "impacted_tests": [],
                "executed": False,
                "passed": True,
                "exit_code": 0,
                "baseline_promoted": False,
                "autopsy_path": None,
                "diff_path": None,
                "reproducer_path": None,
                "output": "",
            }

        start_eid = BaselineManager.get_latest_event_id(self.db_path)
        autopsy_path = os.path.join(self.root_dir, ".blackbox.md")
        exit_code, output = self._execute_impacted_tests(impacted_tests, autopsy_path)

        baseline_promoted = False
        artifacts: Dict[str, Optional[str]] = {
            "autopsy_path": None,
            "diff_path": None,
            "reproducer_path": None,
        }

        if exit_code == 0:
            if self.auto_baseline and os.path.isfile(self.db_path):
                baseline_promoted = BaselineManager.promote_to_baseline(
                    wal_path=self.db_path,
                    baseline_path=self.baseline_path,
                    since_event_id=start_eid,
                )
        else:
            artifacts = self._compile_failure_artifacts(autopsy_path)

        # Refresh snapshot so generated artifacts don't affect subsequent polls
        self._snapshot = self._scan_files()

        return {
            "changed_files": changed_files,
            "impacted_tests": impacted_tests,
            "executed": True,
            "passed": exit_code == 0,
            "exit_code": exit_code,
            "baseline_promoted": baseline_promoted,
            "autopsy_path": artifacts["autopsy_path"],
            "diff_path": artifacts["diff_path"],
            "reproducer_path": artifacts["reproducer_path"],
            "output": output,
        }

    def watch(self, max_iterations: Optional[int] = None) -> List[Dict[str, Any]]:
        """Continuously polls for source modifications until KeyboardInterrupt or max_iterations."""
        results: List[Dict[str, Any]] = []
        iterations = 0
        try:
            while max_iterations is None or iterations < max_iterations:
                res = self.run_once()
                if res["executed"]:
                    results.append(res)
                iterations += 1
                if max_iterations is not None and iterations >= max_iterations:
                    break
                time.sleep(self.interval)
        except KeyboardInterrupt:
            pass
        return results

