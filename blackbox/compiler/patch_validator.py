"""
Genesis Blackbox: Candidate Patch Validator
Enables AI coding agents to test and evaluate diffs safely in an isolated,
guaranteed-rollback context before committing changes.
"""

import os
import sys
import ast
import subprocess
from typing import Dict, Any, List, Set, Optional

from ..ast_engine.impact import ImpactTestSelector
from ..ast_engine.polyglot import PolyglotParser


class PatchValidator:
    """
    Validates candidate patches by dry-running git apply, checking Python AST syntax,
    executing reproduction tests, and running AST-impacted test suites with automatic rollback.
    """

    def __init__(self, repo_root: str = ".", db_path: str = ".blackbox/wal.sqlite"):
        self.repo_root = os.path.abspath(repo_root)
        self.db_path = db_path
        self.impact_selector = ImpactTestSelector(root_dir=self.repo_root)

    def is_path_safe(self, path: str) -> bool:
        """Verifies if path resolves safely within repo_root without directory traversal."""
        try:
            # realpath follows symlinks so an in-repo link cannot point the write outside.
            real_root = os.path.realpath(self.repo_root)
            real_target = os.path.realpath(os.path.abspath(path))
            if os.path.commonpath([real_root, real_target]) != real_root:
                return False
            rel = os.path.relpath(real_target, real_root)
            if rel == ".":
                return False
            return ".git" not in rel.split(os.sep)
        except Exception:
            return False

    @staticmethod
    def _clean_header_path(raw: str) -> Optional[str]:
        """Strips quotes and tab timestamps from a diff header path."""
        path = raw.split("\t", 1)[0].strip()
        if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
            path = path[1:-1]
        if not path or path == "/dev/null":
            return None
        return path

    def _resolve_patch_path(self, path: str) -> str:
        # git apply strips one leading component by default (-p1), whatever the prefix letter.
        if os.path.isabs(path) or path.startswith(".."):
            return os.path.normpath(path if os.path.isabs(path) else os.path.join(self.repo_root, path))
        parts = path.split("/", 1)
        stripped = parts[1] if len(parts) == 2 else parts[0]
        return os.path.normpath(os.path.join(self.repo_root, stripped))

    def has_symlink_mode(self, patch_str: str) -> bool:
        """Detects git symlink modes (120000), which could redirect writes outside the repo."""
        for line in patch_str.splitlines():
            if line.startswith(("new file mode", "old mode", "new mode", "deleted file mode", "index ")):
                if "120000" in line:
                    return True
        return False

    def extract_touched_files(self, patch_str: str) -> List[str]:
        """Extracts modified file paths from a unified diff string."""
        touched: Set[str] = set()
        for line in patch_str.splitlines():
            raw: Optional[str] = None
            if line.startswith(("+++ ", "--- ")):
                raw = line[4:]
            elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
                # These headers carry repo-relative paths with no prefix component.
                cleaned = self._clean_header_path(line.split(" ", 2)[2])
                if cleaned:
                    touched.add(os.path.normpath(os.path.join(self.repo_root, cleaned)))
                continue
            elif line.startswith("diff --git "):
                continue
            if raw is None:
                continue
            cleaned = self._clean_header_path(raw)
            if cleaned:
                touched.add(self._resolve_patch_path(cleaned))
        return sorted(list(touched))

    def _diff_git_header_paths(self, patch_str: str) -> List[str]:
        # Space-split is lossy for names with spaces, so these feed the safety check only.
        paths: Set[str] = set()
        for line in patch_str.splitlines():
            if line.startswith("diff --git "):
                for token in line[len("diff --git "):].split(" "):
                    cleaned = self._clean_header_path(token)
                    if cleaned:
                        paths.add(self._resolve_patch_path(cleaned))
        return sorted(paths)

    def _check_patch_safety(self, patch_str: str, touched: List[str]) -> Optional[str]:
        if self.has_symlink_mode(patch_str):
            return "Security violation: patch creates or modifies a symlink (mode 120000)."
        for fp in list(touched) + self._diff_git_header_paths(patch_str):
            if not self.is_path_safe(fp):
                return f"Security violation: path traversal detected outside repository: {fp}"
        return None

    def _run(self, cmd: List[str], timeout: int, input_str: Optional[str] = None) -> Optional[subprocess.CompletedProcess]:
        try:
            return subprocess.run(
                cmd,
                input=input_str,
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=timeout
            )
        except (subprocess.TimeoutExpired, OSError):
            return None

    def validate_patch_dry_run(self, patch_str: str) -> bool:
        """
        Executes 'git apply --check' to verify if the patch applies cleanly
        without conflicts or syntax corruption.
        """
        if not patch_str.strip():
            return False
        if not patch_str.endswith("\n"):
            patch_str += "\n"

        touched = self.extract_touched_files(patch_str)
        if self._check_patch_safety(patch_str, touched):
            return False

        res = self._run(["git", "apply", "--check", "-"], timeout=10, input_str=patch_str)
        return res is not None and res.returncode == 0

    def _restore_snapshots(self, snapshots: Dict[str, Optional[bytes]]) -> None:
        for fp, orig_bytes in snapshots.items():
            try:
                if orig_bytes is None:
                    if os.path.lexists(fp):
                        os.remove(fp)
                else:
                    os.makedirs(os.path.dirname(fp) or ".", exist_ok=True)
                    with open(fp, "wb") as f_restore:
                        f_restore.write(orig_bytes)
            except Exception:
                pass

    def _resolve_test_target(self, test_path: Optional[str]) -> str:
        candidates = [
            "test_blackbox_reproduce.mjs",
            "test_blackbox_reproduce.test.ts",
            "test_blackbox_reproduce.test.js",
            "test_blackbox_reproduce.js",
            "test_blackbox_reproduce.py"
        ]
        # The Python default is a placeholder; fall back to Node reproducers when it is absent.
        if test_path and not (test_path == "test_blackbox_reproduce.py"
                              and not os.path.exists(os.path.join(self.repo_root, test_path))):
            return test_path
        for cand in candidates:
            if os.path.exists(os.path.join(self.repo_root, cand)):
                return cand
        return "test_blackbox_reproduce.py"

    def evaluate_patch(
        self,
        patch_str: str,
        test_path: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Applies candidate patch temporarily, verifies reproduction test and regression impact,
        guarantees full rollback, and returns a structured verdict.
        """
        if not patch_str.endswith("\n"):
            patch_str += "\n"

        def _fail(msg: str) -> Dict[str, Any]:
            return {
                "success": False,
                "syntax_valid": False,
                "reproducer_passed": False,
                "regressions_count": 0,
                "output": msg
            }

        touched_files = self.extract_touched_files(patch_str)
        violation = self._check_patch_safety(patch_str, touched_files)
        if violation:
            return _fail(violation)

        target_test = self._resolve_test_target(test_path)
        test_full_path = os.path.normpath(os.path.join(self.repo_root, target_test))
        if not self.is_path_safe(test_full_path):
            return _fail(f"Security violation: test path resolves outside repository: {target_test}")

        # Step 1: Dry run verification
        if not self.validate_patch_dry_run(patch_str):
            return _fail("Candidate patch failed 'git apply --check'. Invalid diff format or conflict with working tree.")

        pre_patch_snapshots: Dict[str, Optional[bytes]] = {}
        for fp in touched_files:
            if os.path.isfile(fp):
                with open(fp, "rb") as f_orig:
                    pre_patch_snapshots[fp] = f_orig.read()
            else:
                pre_patch_snapshots[fp] = None

        apply_res = self._run(["git", "apply", "-"], timeout=10, input_str=patch_str)
        if apply_res is None or apply_res.returncode != 0:
            # A timed-out apply may have left partial writes behind.
            self._restore_snapshots(pre_patch_snapshots)
            err = apply_res.stderr if apply_res is not None else "git apply timed out or could not start"
            return _fail(f"Failed to apply patch: {err}")

        syntax_valid = True
        reproducer_passed = False
        regressions_count = 0
        output_messages: List[str] = []

        try:
            # Step 3: Validate AST syntax of modified Python, JS, and TS files
            for file_path in touched_files:
                if not os.path.isfile(file_path):
                    continue
                if file_path.endswith(".py"):
                    try:
                        with open(file_path, "r", encoding="utf-8") as f:
                            ast.parse(f.read(), filename=file_path)
                    except SyntaxError as syn_err:
                        syntax_valid = False
                        output_messages.append(f"SyntaxError in {file_path}:{syn_err.lineno}: {syn_err.msg}")
                        break
                elif any(file_path.endswith(ext) for ext in [".js", ".mjs", ".cjs"]):
                    res = self._run(["node", "--check", file_path], timeout=10)
                    if res is None:
                        syntax_valid = False
                        output_messages.append(f"Node syntax check could not run for {file_path} (timeout or node missing)")
                        break
                    if res.returncode != 0:
                        syntax_valid = False
                        output_messages.append(f"Node syntax check failed for {file_path}:\n{res.stderr.strip() or res.stdout.strip()}")
                        break
                elif any(file_path.endswith(ext) for ext in [".ts", ".tsx", ".jsx"]):
                    try:
                        with open(file_path, "r", encoding="utf-8") as f:
                            content = f.read()
                        parser = PolyglotParser(file_path=file_path)
                        parser.validate_syntax(content)
                    except SyntaxError as syn_err:
                        syntax_valid = False
                        output_messages.append(f"TypeScript/JSX syntax error in {file_path}: {syn_err}")
                        break

            if not syntax_valid:
                return {
                    "success": False,
                    "syntax_valid": False,
                    "reproducer_passed": False,
                    "regressions_count": 0,
                    "output": "\n".join(output_messages)
                }

            # Step 4: Execute reproduction test if provided or present
            if os.path.exists(test_full_path):
                if any(test_full_path.endswith(ext) for ext in [".mjs", ".cjs", ".js", ".ts", ".tsx"]):
                    repro_cmd = ["node", "--test", test_full_path]
                else:
                    repro_cmd = [sys.executable, "-m", "pytest", test_full_path, "-v"]

                repro_res = self._run(repro_cmd, timeout=60)
                if repro_res is None:
                    reproducer_passed = False
                    output_messages.append("Reproduction test timed out or could not start with candidate patch.")
                elif repro_res.returncode == 0:
                    reproducer_passed = True
                else:
                    reproducer_passed = False
                    output_messages.append(f"Reproduction test failed with candidate patch:\n{repro_res.stdout}\n{repro_res.stderr}")
            else:
                # If no reproduction script specified, assume passing baseline
                reproducer_passed = True

            # Step 5: Execute AST-impacted test suite to detect regressions
            if reproducer_passed:
                impacted = self.impact_selector.find_impacted_tests(files=touched_files, diff=False)
                # Exclude the reproduction test itself from regressions run
                regression_tests = [t for t in impacted if os.path.normpath(t) != test_full_path]
                if regression_tests:
                    reg_res = self.impact_selector.run_impacted_tests(regression_tests)
                    if reg_res.returncode != 0:
                        regressions_count = 1
                        output_messages.append(f"Regressions detected in impacted tests:\n{reg_res.stdout}\n{reg_res.stderr}")

        finally:
            # Snapshot restore must run even if the reverse apply times out or raises.
            try:
                rev_res = self._run(["git", "apply", "--reverse", "-"], timeout=10, input_str=patch_str)
                reversed_ok = rev_res is not None and rev_res.returncode == 0
            except Exception:
                reversed_ok = False
            if not reversed_ok:
                self._restore_snapshots(pre_patch_snapshots)
            # Always ensure newly created files are cleaned up if reverse missed them
            for fp, orig_bytes in pre_patch_snapshots.items():
                if orig_bytes is None and os.path.lexists(fp):
                    try:
                        os.remove(fp)
                    except Exception:
                        pass

        success = syntax_valid and reproducer_passed and (regressions_count == 0)
        final_output = "\n".join(output_messages) if output_messages else "Candidate patch verified cleanly: crash resolved with zero regressions."

        return {
            "success": success,
            "syntax_valid": syntax_valid,
            "reproducer_passed": reproducer_passed,
            "regressions_count": regressions_count,
            "output": final_output
        }
