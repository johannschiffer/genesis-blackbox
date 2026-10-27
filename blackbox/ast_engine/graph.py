"""
Genesis Blackbox: AST Dependency & Blast-Radius Engine
Parses Python source files to extract call graphs and determine upstream/downstream blast radius.
"""

import ast
from typing import Dict, Any, List, Set, Optional


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

    def get_blast_radius(self, target_function: str, depth: int = 2) -> Dict[str, Any]:
        """Calculates upstream callers and downstream dependencies."""
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
