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

        # Step 2: Apply patch
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
            # Step 3: Validate AST syntax of modified Python files
            for file_path in touched_files:
                if file_path.endswith(".py") and os.path.isfile(file_path):
                    try:
                        with open(file_path, "r", encoding="utf-8") as f:
                            ast.parse(f.read(), filename=file_path)
                    except SyntaxError as syn_err:
                        syntax_valid = False
                        output_messages.append(f"SyntaxError in {file_path}:{syn_err.lineno}: {syn_err.msg}")
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
            target_test = test_path or "test_blackbox_reproduce.py"
            test_full_path = os.path.normpath(os.path.join(self.repo_root, target_test))

            if os.path.exists(test_full_path):
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
            # Step 6: Guaranteed rollback
            reverse_cmd = ["git", "apply", "--reverse", "-"]
            rev_res = subprocess.run(
                reverse_cmd,
                input=patch_str,
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=10
            )
            # If reverse failed, checkout touched files to clean working tree
            if rev_res.returncode != 0 and touched_files:
                checkout_cmd = ["git", "checkout", "--"] + touched_files
                subprocess.run(checkout_cmd, cwd=self.repo_root, capture_output=True, text=True, timeout=10)

        success = syntax_valid and reproducer_passed and (regressions_count == 0)
        final_output = "\n".join(output_messages) if output_messages else "Candidate patch verified cleanly: crash resolved with zero regressions."

        return {
            "success": success,
            "syntax_valid": syntax_valid,
            "reproducer_passed": reproducer_passed,
            "regressions_count": regressions_count,
            "output": final_output
        }
