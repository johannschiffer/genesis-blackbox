"""
Genesis Blackbox: AST Dependency & Blast-Radius Engine
Parses Python source files to extract call graphs and determine upstream/downstream blast radius.
"""

import os
import ast
import subprocess
from typing import Dict, Any, List, Set, Optional
from .polyglot import PolyglotParser

POLYGLOT_EXTENSIONS = (".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
JS_TS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


class FunctionNodeVisitor(ast.NodeVisitor):
    def __init__(self, filename: str):
        self.filename = filename
        self.functions: Dict[str, Dict[str, Any]] = {}
        self.current_function: Optional[str] = None

    def visit_FunctionDef(self, node: ast.FunctionDef):
        func_name = node.name
        self.functions[func_name] = {
            "name": func_name,
            "filename": self.filename,
            "lineno": node.lineno,
            "end_lineno": getattr(node, "end_lineno", node.lineno),
            "calls": set(),
            "args": [a.arg for a in node.args.args]
        }
        prev = self.current_function
        self.current_function = func_name
        self.generic_visit(node)
        self.current_function = prev

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        self.visit_FunctionDef(node)  # type: ignore

    def visit_Call(self, node: ast.Call):
        if self.current_function:
            callee = None
            if isinstance(node.func, ast.Name):
                callee = node.func.id
            elif isinstance(node.func, ast.Attribute):
                callee = node.func.attr
            if callee:
                self.functions[self.current_function]["calls"].add(callee)
        self.generic_visit(node)


class CodebaseGraph:
    """Computes call dependencies and calculates blast radius of changes."""

    def __init__(self):
        self.nodes: Dict[str, Dict[str, Any]] = {}
        self.callers_map: Dict[str, Set[str]] = {}

    def parse_source(self, code_str: str, filename: str = "<source>") -> None:
        if filename.endswith(JS_TS_EXTENSIONS):
            try:
                parser = PolyglotParser(filename)
                res = parser.parse(code_str)
                for f_name, data in res["functions"].items():
                    self.nodes[f_name] = data
                    for callee in data["calls"]:
                        if callee not in self.callers_map:
                            self.callers_map[callee] = set()
                        self.callers_map[callee].add(f_name)
            except Exception:
                pass
            return

        try:
            tree = ast.parse(code_str, filename=filename)
            visitor = FunctionNodeVisitor(filename)
            visitor.visit(tree)
            for f_name, data in visitor.functions.items():
                self.nodes[f_name] = data
                for callee in data["calls"]:
                    if callee not in self.callers_map:
                        self.callers_map[callee] = set()
                    self.callers_map[callee].add(f_name)
        except Exception:
            pass

    def parse_file(self, file_path: str) -> bool:
        """Parses a single Python file from disk."""
        if not os.path.isfile(file_path):
            return False
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            self.parse_source(content, filename=file_path)
            return True
        except Exception:
            return False

    def parse_directory(self, dir_path: str, max_files: int = 50) -> int:
        """Parses all Python and TypeScript/JavaScript files within a directory up to max_files."""
        excluded_dirs = {
            ".venv", "venv", ".git", "node_modules", "__pycache__",
            ".pytest_cache", "build", "dist", ".tox", ".mypy_cache"
        }
        count = 0
        for root, dirs, files in os.walk(dir_path):
            dirs[:] = [d for d in dirs if d not in excluded_dirs and not d.endswith(".egg-info")]
            for file in files:
                if file.endswith(POLYGLOT_EXTENSIONS):
                    full_path = os.path.join(root, file)
                    if self.parse_file(full_path):
                        count += 1
                        if count >= max_files:
                            return count
        return count

    def get_blast_radius(
        self,
        target_function: str,
        depth: int = 2,
        file_path: Optional[str] = None
    ) -> Dict[str, Any]:
        """Calculates upstream callers and downstream dependencies."""
        if file_path and target_function not in self.nodes:
            self.parse_file(file_path)

        upstream = set()
        downstream = set()

        def _walk_callers(name: str, cur_depth: int):
            if cur_depth > depth:
                return
            for caller in self.callers_map.get(name, []):
                if caller not in upstream:
                    upstream.add(caller)
                    _walk_callers(caller, cur_depth + 1)

        def _walk_callees(name: str, cur_depth: int):
            if cur_depth > depth:
                return
            node = self.nodes.get(name)
            if node:
                for callee in node.get("calls", []):
                    if callee not in downstream:
                        downstream.add(callee)
                        _walk_callees(callee, cur_depth + 1)

        _walk_callers(target_function, 1)
        _walk_callees(target_function, 1)

        return {
            "target": target_function,
            "upstream_callers": sorted(list(upstream)),
            "downstream_callees": sorted(list(downstream)),
            "total_blast_radius": len(upstream) + len(downstream)
        }

    @staticmethod
    def get_git_diff(files: List[str], repo_root: Optional[str] = None) -> str:
        """Extracts recent git diff for the specified files."""
        valid_files = [f for f in files if f and os.path.exists(f)]
        if not valid_files:
            return ""
        try:
            cwd = repo_root or os.path.dirname(os.path.abspath(valid_files[0]))
            cmd = ["git", "diff", "HEAD~1", "--"] + valid_files
            res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=5)
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
            # Fallback to uncommitted git diff
            cmd_uncommitted = ["git", "diff", "--"] + valid_files
            res2 = subprocess.run(cmd_uncommitted, cwd=cwd, capture_output=True, text=True, timeout=5)
            if res2.returncode == 0 and res2.stdout.strip():
                return res2.stdout.strip()
        except Exception:
            pass
        return ""

