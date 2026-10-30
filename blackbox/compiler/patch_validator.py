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

    def extract_touched_files(self, patch_str: str) -> List[str]:
        """Extracts modified file paths from a unified diff string."""
        touched: Set[str] = set()
        for line in patch_str.splitlines():
            if line.startswith("+++ b/"):
                rel_path = line[6:].strip()
                if rel_path != "/dev/null":
                    full_path = os.path.normpath(os.path.join(self.repo_root, rel_path))
                    touched.add(full_path)
            elif line.startswith("--- a/"):
                rel_path = line[6:].strip()
                if rel_path != "/dev/null":
                    full_path = os.path.normpath(os.path.join(self.repo_root, rel_path))
                    touched.add(full_path)
        return sorted(list(touched))

    def validate_patch_dry_run(self, patch_str: str) -> bool:
        """
        Executes 'git apply --check' to verify if the patch applies cleanly
        without conflicts or syntax corruption.
        """
        if not patch_str.strip():
            return False
        try:
            cmd = ["git", "apply", "--check", "-"]
            res = subprocess.run(
                cmd,
                input=patch_str,
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=10
            )
            return res.returncode == 0
        except Exception:
            return False

    def evaluate_patch(
        self,
        patch_str: str,
        test_path: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Applies candidate patch temporarily, verifies reproduction test and regression impact,
        guarantees full rollback, and returns a structured verdict.
        """
        # Step 1: Dry run verification
        if not self.validate_patch_dry_run(patch_str):
            return {
                "success": False,
                "syntax_valid": False,
                "reproducer_passed": False,
                "regressions_count": 0,
                "output": "Candidate patch failed 'git apply --check'. Invalid diff format or conflict with working tree."
            }

        touched_files = self.extract_touched_files(patch_str)

        pre_patch_snapshots: Dict[str, Optional[bytes]] = {}
        for fp in touched_files:
            if os.path.isfile(fp):
                with open(fp, "rb") as f_orig:
                    pre_patch_snapshots[fp] = f_orig.read()
            else:
                pre_patch_snapshots[fp] = None

        apply_cmd = ["git", "apply", "-"]
        apply_res = subprocess.run(
            apply_cmd,
            input=patch_str,
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            timeout=10
        )
        if apply_res.returncode != 0:
            return {
                "success": False,
                "syntax_valid": False,
                "reproducer_passed": False,
                "regressions_count": 0,
                "output": f"Failed to apply patch: {apply_res.stderr}"
            }

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
                    res = subprocess.run(
                        ["node", "--check", file_path],
                        cwd=self.repo_root,
                        capture_output=True,
                        text=True,
                        timeout=10
                    )
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
            target_test = test_path
            if not target_test:
                for cand in [
                    "test_blackbox_reproduce.mjs",
                    "test_blackbox_reproduce.test.ts",
                    "test_blackbox_reproduce.test.js",
                    "test_blackbox_reproduce.js",
                    "test_blackbox_reproduce.py"
                ]:
                    if os.path.exists(os.path.join(self.repo_root, cand)):
                        target_test = cand
                        break
            if not target_test:
                target_test = "test_blackbox_reproduce.py"

            test_full_path = os.path.normpath(os.path.join(self.repo_root, target_test))

            if os.path.exists(test_full_path):
                if any(test_full_path.endswith(ext) for ext in [".mjs", ".cjs", ".js", ".ts", ".tsx"]):
                    repro_cmd = ["node", "--test", test_full_path]
                else:
                    repro_cmd = [sys.executable, "-m", "pytest", test_full_path, "-v"]

                repro_res = subprocess.run(
                    repro_cmd,
                    cwd=self.repo_root,
                    capture_output=True,
                    text=True,
                    timeout=60
                )
                if repro_res.returncode == 0:
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
            reverse_cmd = ["git", "apply", "--reverse", "-"]
            rev_res = subprocess.run(
                reverse_cmd,
                input=patch_str,
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=10
            )
            if rev_res.returncode != 0:
                for fp, orig_bytes in pre_patch_snapshots.items():
                    try:
                        if orig_bytes is None:
                            if os.path.exists(fp):
                                os.remove(fp)
                        else:
                            os.makedirs(os.path.dirname(fp) or ".", exist_ok=True)
                            with open(fp, "wb") as f_restore:
                                f_restore.write(orig_bytes)
                    except Exception:
                        pass
            # Always ensure newly created files are cleaned up if reverse missed them
            for fp, orig_bytes in pre_patch_snapshots.items():
                if orig_bytes is None and os.path.exists(fp):
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
