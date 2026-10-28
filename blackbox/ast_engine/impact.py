"""
Genesis Blackbox: AST Impact Test Selector
Selects and executes only test files affected by recent changes or diffs
using AST symbol analysis and upstream dependency tracing.
"""

import os
import sys
import ast
import subprocess
from typing import Dict, Any, List, Set, Optional
from .graph import CodebaseGraph


class TestDependencyVisitor(ast.NodeVisitor):
    """Inspects imports and function calls within a test file."""

    def __init__(self, filename: str):
        self.filename = filename
        self.imported_modules: Set[str] = set()
        self.imported_symbols: Set[str] = set()
        self.called_symbols: Set[str] = set()
        self.test_functions: List[str] = []

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            self.imported_modules.add(alias.name)
            if alias.asname:
                self.imported_symbols.add(alias.asname)
            else:
                self.imported_symbols.add(alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        mod = node.module or ""
        self.imported_modules.add(mod)
        for alias in node.names:
            self.imported_symbols.add(alias.name)
            if alias.asname:
                self.imported_symbols.add(alias.asname)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef):
        if node.name.startswith("test_") or node.name.endswith("_test"):
            self.test_functions.append(node.name)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call):
        if isinstance(node.func, ast.Name):
            self.called_symbols.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            self.called_symbols.add(node.func.attr)
        self.generic_visit(node)


class ImpactTestSelector:
    """
    Selects test files impacted by changes in source files
    by cross-referencing AST symbol definitions and test imports/calls.
    """

    def __init__(self, root_dir: str = ".", graph: Optional[CodebaseGraph] = None):
        self.root_dir = os.path.abspath(root_dir)
        self.graph = graph or CodebaseGraph()

    def get_modified_files_from_git(self) -> List[str]:
        """Retrieves list of modified Python files from git working tree."""
        modified = []
        try:
            # 1. Unstaged and staged files
            cmd_status = ["git", "status", "--porcelain"]
            res = subprocess.run(cmd_status, cwd=self.root_dir, capture_output=True, text=True, timeout=5)
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    parts = line.strip().split()
                    if len(parts) >= 2:
                        filepath = parts[-1]
                        if filepath.endswith(".py"):
                            full_path = os.path.normpath(os.path.join(self.root_dir, filepath))
                            if os.path.exists(full_path) and full_path not in modified:
                                modified.append(full_path)

            # 2. If nothing unstaged/staged, check diff against HEAD~1
            if not modified:
                cmd_diff = ["git", "diff", "--name-only", "HEAD~1"]
                res_diff = subprocess.run(cmd_diff, cwd=self.root_dir, capture_output=True, text=True, timeout=5)
                if res_diff.returncode == 0:
                    for line in res_diff.stdout.splitlines():
                        filepath = line.strip()
                        if filepath.endswith(".py"):
                            full_path = os.path.normpath(os.path.join(self.root_dir, filepath))
                            if os.path.exists(full_path) and full_path not in modified:
                                modified.append(full_path)
        except Exception:
            pass
        return modified

    def extract_symbols_from_file(self, file_path: str) -> Set[str]:
        """Extracts defined function and class names from a Python source file."""
        symbols = set()
        if not os.path.isfile(file_path):
            return symbols
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                tree = ast.parse(f.read(), filename=file_path)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    symbols.add(node.name)
        except Exception:
            pass
        return symbols

    def discover_test_files(self, search_dir: Optional[str] = None) -> List[str]:
        """Discovers all test_*.py and *_test.py files in the target directory."""
        base = search_dir or self.root_dir
        test_files = []
        for root, dirs, files in os.walk(base):
            # Exclude virtual environments and cache directories
            dirs[:] = [d for d in dirs if d not in (".venv", "venv", ".git", "__pycache__", ".pytest_cache")]
            for f in files:
                if (f.startswith("test_") or f.endswith("_test.py")) and f.endswith(".py"):
                    test_files.append(os.path.normpath(os.path.join(root, f)))
        return sorted(test_files)

    def find_impacted_tests(
        self,
        files: Optional[List[str]] = None,
        diff: bool = True,
        tests_dir: Optional[str] = None
    ) -> List[str]:
        """
        Calculates tests that directly or indirectly test the altered source files.
        Returns a sorted list of impacted test file paths.
        """
        target_files = files
        if not target_files and diff:
            target_files = self.get_modified_files_from_git()

        if not target_files:
            return []

        # Normalize target file paths
        normalized_targets = [os.path.normpath(os.path.abspath(f)) for f in target_files if f]

        impacted_tests: Set[str] = set()

        # Step 1: If target file is itself a test file, it is directly impacted
        for f in normalized_targets:
            base_name = os.path.basename(f)
            if base_name.startswith("test_") or base_name.endswith("_test.py"):
                impacted_tests.add(f)

        source_symbols: Set[str] = set()
        source_modules: Set[str] = set()
        source_basenames: Set[str] = set()

        if not self.graph.nodes:
            self.graph.parse_directory(self.root_dir, max_files=200)

        for f in normalized_targets:
            base_no_ext = os.path.splitext(os.path.basename(f))[0]
            source_basenames.add(base_no_ext)
            source_symbols.update(self.extract_symbols_from_file(f))

            # Also resolve upstream callers in AST graph
            for sym in self.extract_symbols_from_file(f):
                radius = self.graph.get_blast_radius(sym, file_path=f)
                for caller in radius.get("upstream_callers", []):
                    source_symbols.add(caller)

            # Compute possible module import paths
            rel_path = os.path.relpath(f, self.root_dir)
            mod_path = rel_path.replace(os.sep, ".").replace(".py", "")
            source_modules.add(mod_path)
            if "." in mod_path:
                source_modules.add(mod_path.split(".")[-1])

        # Step 3: Inspect all discovered test files
        all_tests = self.discover_test_files(tests_dir)

        for test_file in all_tests:
            # Check name conventions (e.g., test_calculator.py maps to calculator.py)
            test_base = os.path.splitext(os.path.basename(test_file))[0]
            for src_base in source_basenames:
                if test_base == f"test_{src_base}" or test_base == f"{src_base}_test" or src_base in test_base:
                    impacted_tests.add(test_file)
                    break

            if test_file in impacted_tests:
                continue

            # Parse test file AST to inspect imports and calls
            try:
                with open(test_file, "r", encoding="utf-8", errors="replace") as tf:
                    tree = ast.parse(tf.read(), filename=test_file)
                visitor = TestDependencyVisitor(test_file)
                visitor.visit(tree)

                # Check if test imports any target module
                if any(m in visitor.imported_modules for m in source_modules):
                    impacted_tests.add(test_file)
                    continue

                # Check if test imports or calls any target symbol
                if any(s in visitor.imported_symbols or s in visitor.called_symbols for s in source_symbols):
                    impacted_tests.add(test_file)
                    continue
            except Exception:
                pass

        return sorted(list(impacted_tests))

    def run_impacted_tests(self, test_nodes: List[str]) -> subprocess.CompletedProcess:
        """Executes pytest against the specified impacted test nodes."""
        if not test_nodes:
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="No tests to run.", stderr="")
        cmd = [sys.executable, "-m", "pytest"] + test_nodes + ["-v"]
        return subprocess.run(cmd, cwd=self.root_dir, capture_output=True, text=True)
