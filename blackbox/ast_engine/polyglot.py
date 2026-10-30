"""
Genesis Blackbox: Polyglot Lexical & Structural AST Engine
Pure-Python parser supporting TypeScript (.ts, .tsx) and JavaScript (.js, .jsx)
for symbol extraction, dependency graph generation, and blast-radius calculation.
"""

import os
import re
from typing import Dict, Any, List, Set, Optional, Tuple


class PolyglotParser:
    """
    Lightweight lexical and structural parser in pure Python for TypeScript,
    JavaScript, JSX, and TSX source files.
    Extracts imports, exports, functions, methods, and React/Next.js boundaries.
    """

    JS_KEYWORDS = {
        "if", "else", "for", "while", "do", "switch", "case", "default",
        "break", "continue", "return", "try", "catch", "finally", "throw",
        "typeof", "instanceof", "in", "of", "new", "delete", "void", "this",
        "super", "class", "extends", "function", "const", "let", "var",
        "import", "export", "from", "as", "async", "await", "yield"
    }

    def __init__(self, file_path: str = "<source>"):
        self.file_path = file_path
        self.imports: List[Dict[str, Any]] = []
        self.exports: List[Dict[str, Any]] = []
        self.functions: Dict[str, Dict[str, Any]] = {}
        self.imported_modules: Set[str] = set()
        self.imported_symbols: Set[str] = set()
        self.defined_symbols: Set[str] = set()

    def parse(self, code_str: str) -> Dict[str, Any]:
        """Parses JavaScript/TypeScript source code."""
        cleaned_code, line_offsets = self._strip_comments_and_strings(code_str)
        self._extract_imports(code_str, cleaned_code)
        self._extract_exports(code_str, cleaned_code)
        self._extract_functions_and_components(code_str, cleaned_code, line_offsets)
        return {
            "filename": self.file_path,
            "imports": self.imports,
            "exports": self.exports,
            "functions": self.functions,
            "imported_modules": sorted(list(self.imported_modules)),
            "imported_symbols": sorted(list(self.imported_symbols)),
            "defined_symbols": sorted(list(self.defined_symbols))
        }

    def validate_syntax(self, code_str: str) -> None:
        """
        Validates structural syntax of TypeScript / JavaScript source code.
        Verifies balanced brackets, parentheses, braces, and closed string/template literals.
        Raises SyntaxError if structural syntax is invalid.
        """
        cleaned, line_offsets = self._strip_comments_and_strings(code_str)
        # Check bracket/brace/parenthesis nesting
        stack: List[Tuple[str, int]] = []
        matching = {")": "(", "}": "{", "]": "["}
        for idx, char in enumerate(cleaned):
            if char in "({[":
                lineno = self._offset_to_lineno(idx, line_offsets)
                stack.append((char, lineno))
            elif char in ")}]":
                lineno = self._offset_to_lineno(idx, line_offsets)
                if not stack:
                    raise SyntaxError(f"Unexpected closing '{char}' at line {lineno}")
                top, top_line = stack.pop()
                if matching[char] != top:
                    raise SyntaxError(f"Mismatched closing '{char}' at line {lineno}, expected matching '{top}' opened at line {top_line}")
        if stack:
            top, top_line = stack[-1]
            raise SyntaxError(f"Unclosed '{top}' opened at line {top_line}")

        # Parse symbols and structure
        self.parse(code_str)

    def _strip_comments_and_strings(self, code: str) -> Tuple[str, List[int]]:
        """
        Replaces comment characters and string contents with whitespace,
        preserving newlines and exact character offsets.
        Returns (cleaned_code, line_start_offsets).
        """
        chars = list(code)
        n = len(chars)
        i = 0
        in_single_quote = False
        in_double_quote = False
        in_template_literal = False
        in_line_comment = False
        in_block_comment = False

        line_offsets = [0]

        while i < n:
            c = chars[i]
            next_c = chars[i + 1] if i + 1 < n else ""

            if c == "\n":
                line_offsets.append(i + 1)
                if in_line_comment:
                    in_line_comment = False
                i += 1
                continue

            if in_line_comment:
                chars[i] = " "
                i += 1
                continue

            if in_block_comment:
                if c == "*" and next_c == "/":
                    chars[i] = " "
                    chars[i + 1] = " "
                    in_block_comment = False
                    i += 2
                else:
                    chars[i] = " "
                    i += 1
                continue

            if in_single_quote:
                if c == "\\" and i + 1 < n:
                    chars[i] = " "
                    chars[i + 1] = " "
                    i += 2
                elif c == "'":
                    chars[i] = " "
                    in_single_quote = False
                    i += 1
                else:
                    chars[i] = " "
                    i += 1
                continue

            if in_double_quote:
                if c == "\\" and i + 1 < n:
                    chars[i] = " "
                    chars[i + 1] = " "
                    i += 2
                elif c == '"':
                    chars[i] = " "
                    in_double_quote = False
                    i += 1
                else:
                    chars[i] = " "
                    i += 1
                continue

            if in_template_literal:
                if c == "\\" and i + 1 < n:
                    chars[i] = " "
                    chars[i + 1] = " "
                    i += 2
                elif c == "`":
                    chars[i] = " "
                    in_template_literal = False
                    i += 1
                elif c == "$" and next_c == "{":
                    chars[i] = "$"
                    chars[i + 1] = "{"
                    i += 2
                else:
                    chars[i] = " "
                    i += 1
                continue

            # Start comments or strings
            if c == "/" and next_c == "/":
                chars[i] = " "
                chars[i + 1] = " "
                in_line_comment = True
                i += 2
            elif c == "/" and next_c == "*":
                chars[i] = " "
                chars[i + 1] = " "
                in_block_comment = True
                i += 2
            elif c == "'":
                chars[i] = " "
                in_single_quote = True
                i += 1
            elif c == '"':
                chars[i] = " "
                in_double_quote = True
                i += 1
            elif c == "`":
                chars[i] = " "
                in_template_literal = True
                i += 1
            else:
                i += 1

        if in_single_quote:
            raise SyntaxError("Unclosed single-quote string literal")
        if in_double_quote:
            raise SyntaxError("Unclosed double-quote string literal")
        if in_template_literal:
            raise SyntaxError("Unclosed template literal")
        if in_block_comment:
            raise SyntaxError("Unclosed block comment")

        return "".join(chars), line_offsets

    def _offset_to_lineno(self, offset: int, line_offsets: List[int]) -> int:
        """Converts character offset to 1-based line number."""
        import bisect
        idx = bisect.bisect_right(line_offsets, offset)
        return idx

    def _extract_imports(self, raw_code: str, cleaned_code: str) -> None:
        """Extracts ES6 and CommonJS import declarations."""
        # 1. ES6 static imports: import ... from '...'
        es6_pattern = re.compile(
            r'import\s+(?:type\s+)?(?:([a-zA-Z0-9_$]+)\s*,?\s*)?(?:(\*\s+as\s+[a-zA-Z0-9_$]+)|(?:\{\s*([^}]+)\s*\}))?\s*from\s*[\'"]([^\'"]+)[\'"]',
            re.MULTILINE
        )
        for m in es6_pattern.finditer(raw_code):
            default_import = m.group(1)
            ns_import = m.group(2)
            named_imports = m.group(3)
            module_source = m.group(4)

            self.imported_modules.add(module_source)
            symbols = []
            if default_import:
                sym = default_import.strip()
                if sym and sym != "type":
                    symbols.append(sym)
                    self.imported_symbols.add(sym)
            if ns_import:
                ns_match = re.search(r'as\s+([a-zA-Z0-9_$]+)', ns_import)
                if ns_match:
                    sym = ns_match.group(1).strip()
                    symbols.append(sym)
                    self.imported_symbols.add(sym)
            if named_imports:
                for item in named_imports.split(","):
                    item = item.strip()
                    if not item:
                        continue
                    if " as " in item:
                        alias = item.split(" as ")[-1].strip()
                        symbols.append(alias)
                        self.imported_symbols.add(alias)
                    else:
                        sym = item.strip()
                        symbols.append(sym)
                        self.imported_symbols.add(sym)

            self.imports.append({
                "module": module_source,
                "symbols": symbols,
                "is_relative": module_source.startswith(".")
            })

        # 2. Side-effect imports: import '...'
        side_effect_pattern = re.compile(r'import\s+[\'"]([^\'"]+)[\'"]', re.MULTILINE)
        for m in side_effect_pattern.finditer(raw_code):
            mod = m.group(1)
            self.imported_modules.add(mod)

        # 3. CommonJS require: const ... = require('...')
        cjs_pattern = re.compile(
            r'(?:const|let|var)\s+(?:([a-zA-Z0-9_$]+)|\{\s*([^}]+)\s*\})\s*=\s*require\(\s*[\'"]([^\'"]+)[\'"]\s*\)',
            re.MULTILINE
        )
        for m in cjs_pattern.finditer(raw_code):
            default_sym = m.group(1)
            named_syms = m.group(2)
            mod_source = m.group(3)

            self.imported_modules.add(mod_source)
            symbols = []
            if default_sym:
                sym = default_sym.strip()
                symbols.append(sym)
                self.imported_symbols.add(sym)
            if named_syms:
                for item in named_syms.split(","):
                    item = item.strip()
                    if not item:
                        continue
                    if ":" in item:
                        alias = item.split(":")[-1].strip()
                        symbols.append(alias)
                        self.imported_symbols.add(alias)
                    else:
                        symbols.append(item)
                        self.imported_symbols.add(item)

            self.imports.append({
                "module": mod_source,
                "symbols": symbols,
                "is_relative": mod_source.startswith(".")
            })

    def _extract_exports(self, raw_code: str, cleaned_code: str) -> None:
        """Extracts exported symbol names."""
        # export default function/class/variable
        exp_def = re.compile(r'export\s+default\s+(?:(?:async\s+)?function\s*\*?\s*([a-zA-Z0-9_$]+)|class\s+([a-zA-Z0-9_$]+)|([a-zA-Z0-9_$]+))')
        for m in exp_def.finditer(raw_code):
            name = m.group(1) or m.group(2) or m.group(3)
            if name and name not in ("function", "class"):
                self.exports.append({"name": name, "is_default": True})
                self.defined_symbols.add(name)

        # export function/class/const/let
        exp_named = re.compile(r'export\s+(?:async\s+)?(?:function\s*\*?|class|(?:const|let|var))\s+([a-zA-Z0-9_$]+)')
        for m in exp_named.finditer(raw_code):
            name = m.group(1)
            self.exports.append({"name": name, "is_default": False})
            self.defined_symbols.add(name)

        # export { a, b as c }
        exp_list = re.compile(r'export\s*\{\s*([^}]+)\s*\}')
        for m in exp_list.finditer(raw_code):
            for item in m.group(1).split(","):
                item = item.strip()
                if not item:
                    continue
                if " as " in item:
                    alias = item.split(" as ")[-1].strip()
                    self.exports.append({"name": alias, "is_default": False})
                    self.defined_symbols.add(alias)
                else:
                    self.exports.append({"name": item, "is_default": False})
                    self.defined_symbols.add(item)

    def _extract_functions_and_components(
        self,
        raw_code: str,
        cleaned_code: str,
        line_offsets: List[int]
    ) -> None:
        """Extracts function, method, and React/Next.js component declarations."""
        n = len(cleaned_code)

        # Regex patterns for function headers
        # 1. Standard function: function foo(a, b) {
        fn_decl_regex = re.compile(
            r'(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([a-zA-Z0-9_$]+)?\s*\(([^)]*)\)',
            re.MULTILINE
        )

        # 2. Variable arrow or function: const foo = async (a, b) => {  OR  const foo = function(a, b) {
        var_fn_regex = re.compile(
            r'(?:export\s+)?(?:const|let|var)\s+([a-zA-Z0-9_$]+)\s*=\s*(?:async\s+)?(?:function\s*\*?\s*\(([^)]*)\)|\(([^)]*)\)\s*(?::\s*[^=]+)?\s*=>|([a-zA-Z0-9_$]+)\s*=>)',
            re.MULTILINE
        )

        # 3. Class declarations and methods
        class_regex = re.compile(r'class\s+([a-zA-Z0-9_$]+)(?:\s+extends\s+[a-zA-Z0-9_$]+)?\s*\{')

        # Find all function declarations
        for m in fn_decl_regex.finditer(cleaned_code):
            name = m.group(1) or "default"
            raw_args = m.group(2) or ""
            start_offset = m.start()
            header_end = m.end()

            # Find opening brace for body
            body_start = cleaned_code.find("{", header_end)
            if body_start != -1 and body_start - header_end < 100:
                body_end = self._find_matching_brace(cleaned_code, body_start)
                if body_end != -1:
                    self._record_function(
                        name=name,
                        raw_args=raw_args,
                        start_offset=start_offset,
                        end_offset=body_end,
                        body_code=raw_code[body_start + 1:body_end],
                        line_offsets=line_offsets
                    )

        # Find variable assigned functions
        for m in var_fn_regex.finditer(cleaned_code):
            name = m.group(1)
            raw_args = m.group(2) or m.group(3) or m.group(4) or ""
            start_offset = m.start()
            header_end = m.end()

            # Find opening brace or single expression arrow body
            after_arrow = cleaned_code[header_end:].lstrip()
            if after_arrow.startswith("{"):
                body_start = cleaned_code.find("{", header_end)
                body_end = self._find_matching_brace(cleaned_code, body_start)
                if body_end != -1:
                    self._record_function(
                        name=name,
                        raw_args=raw_args,
                        start_offset=start_offset,
                        end_offset=body_end,
                        body_code=raw_code[body_start + 1:body_end],
                        line_offsets=line_offsets
                    )
            else:
                # Single expression arrow: const add = (a, b) => a + b;
                semi_pos = cleaned_code.find(";", header_end)
                nl_pos = cleaned_code.find("\n", header_end)
                end_pos = min([p for p in (semi_pos, nl_pos, n) if p != -1])
                self._record_function(
                    name=name,
                    raw_args=raw_args,
                    start_offset=start_offset,
                    end_offset=end_pos,
                    body_code=raw_code[header_end:end_pos],
                    line_offsets=line_offsets
                )

        # Find class methods
        for cm in class_regex.finditer(cleaned_code):
            class_name = cm.group(1)
            self.defined_symbols.add(class_name)
            class_body_start = cm.end() - 1
            class_body_end = self._find_matching_brace(cleaned_code, class_body_start)
            if class_body_end == -1:
                continue

            class_body = cleaned_code[class_body_start + 1:class_body_end]
            class_raw_body = raw_code[class_body_start + 1:class_body_end]

            method_regex = re.compile(
                r'(?:(?:public|private|protected|static|async|override|get|set)\s+)*([a-zA-Z0-9_$]+)\s*\(([^)]*)\)\s*(?::\s*[^\{]+)?\s*\{'
            )
            for mm in method_regex.finditer(class_body):
                m_name = mm.group(1)
                if m_name in self.JS_KEYWORDS:
                    continue
                raw_args = mm.group(2)
                m_start = class_body_start + 1 + mm.start()
                m_body_start = class_body_start + 1 + mm.end() - 1
                m_body_end = self._find_matching_brace(cleaned_code, m_body_start)
                if m_body_end != -1:
                    qualified_name = f"{class_name}.{m_name}"
                    self._record_function(
                        name=qualified_name,
                        raw_args=raw_args,
                        start_offset=m_start,
                        end_offset=m_body_end,
                        body_code=raw_code[m_body_start + 1:m_body_end],
                        line_offsets=line_offsets
                    )
                    # Also register unqualified method name if not already defined
                    if m_name not in self.functions:
                        self.functions[m_name] = self.functions[qualified_name]

    def _find_matching_brace(self, text: str, open_brace_idx: int) -> int:
        """Finds closing brace matching opening brace at open_brace_idx."""
        depth = 0
        n = len(text)
        for i in range(open_brace_idx, n):
            c = text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return i
        return -1

    def _parse_arguments(self, raw_args: str) -> List[str]:
        """Extracts clean parameter names stripped of types and defaults."""
        args: List[str] = []
        if not raw_args.strip():
            return args

        # Split top-level commas (handling potential braces)
        parts = []
        cur = []
        depth = 0
        for ch in raw_args:
            if ch in "({[":
                depth += 1
            elif ch in ")}]":
                depth -= 1
            elif ch == "," and depth == 0:
                parts.append("".join(cur).strip())
                cur = []
                continue
            cur.append(ch)
        if cur:
            parts.append("".join(cur).strip())

        for p in parts:
            # Strip type annotation: foo: string -> foo
            # Strip default: foo = 5 -> foo
            m = re.match(r'^(?:(?:public|private|protected|readonly)\s+)?([a-zA-Z0-9_$]+)', p)
            if m:
                arg_name = m.group(1)
                if arg_name not in self.JS_KEYWORDS:
                    args.append(arg_name)
            elif p.startswith("{") or p.startswith("["):
                # Destructured parameter
                for sub in re.findall(r'([a-zA-Z0-9_$]+)', p):
                    if sub not in self.JS_KEYWORDS:
                        args.append(sub)
        return args

    def _extract_callees(self, body_code: str) -> Set[str]:
        """Extracts called function and component symbol names within body."""
        callees: Set[str] = set()

        # 1. Standard function call: name(...) or obj.method(...)
        call_pattern = re.compile(r'(?:([a-zA-Z0-9_$]+)\.)?([a-zA-Z0-9_$]+)\s*\(')
        for m in call_pattern.finditer(body_code):
            caller_obj = m.group(1)
            method_name = m.group(2)
            if method_name not in self.JS_KEYWORDS:
                callees.add(method_name)

        # 2. JSX Tag invocation: <ComponentName ... or <ComponentName>
        jsx_tag_pattern = re.compile(r'<([A-Z][a-zA-Z0-9_$]*)(?:[\s/>])')
        for m in jsx_tag_pattern.finditer(body_code):
            comp_name = m.group(1)
            callees.add(comp_name)

        return callees

    def _record_function(
        self,
        name: str,
        raw_args: str,
        start_offset: int,
        end_offset: int,
        body_code: str,
        line_offsets: List[int]
    ) -> None:
        """Constructs function metadata record compatible with CodebaseGraph."""
        lineno = self._offset_to_lineno(start_offset, line_offsets)
        end_lineno = self._offset_to_lineno(end_offset, line_offsets)
        args = self._parse_arguments(raw_args)
        calls = self._extract_callees(body_code)

        # React component & Next.js route boundaries
        is_component = False
        is_route_handler = False

        # PascalCase or JSX / hook presence
        if name and name[0].isupper() and not name.startswith("NEXT_"):
            is_component = True
        elif any(k in body_code for k in ("useState", "useEffect", "useRouter", "useMemo", "useCallback")):
            is_component = True
        elif "<" in body_code and "/>" in body_code:
            is_component = True

        # Next.js App Router route handlers
        base_name = os.path.basename(self.file_path)
        if base_name in ("route.ts", "route.js", "route.tsx", "route.jsx"):
            if name in ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
                is_route_handler = True
        elif name in ("getServerSideProps", "getStaticProps", "getStaticPaths"):
            is_route_handler = True

        self.functions[name] = {
            "name": name,
            "filename": self.file_path,
            "lineno": lineno,
            "end_lineno": end_lineno,
            "calls": calls,
            "args": args,
            "is_component": is_component,
            "is_route_handler": is_route_handler
        }
        self.defined_symbols.add(name)


def parse_v8_stack_trace(stderr_text: str) -> Optional[Dict[str, Any]]:
    """
    Parses V8 / Node.js uncaught error output and stack trace into standard
    Blackbox EXCEPTION payload schema with root cause and frame locals.
    """
    if not stderr_text or not stderr_text.strip():
        return None

    lines = stderr_text.strip().splitlines()

    # 1. Look for Error type and message
    # Examples:
    #   TypeError: Cannot read properties of undefined (reading 'userId')
    #   ReferenceError: foo is not defined
    #   Error: connect ECONNREFUSED 127.0.0.1:5432
    err_pattern = re.compile(r'^([A-Z][a-zA-Z0-9_$]*(?:Error|Exception)|Error):\s*(.*)$')
    exc_type = "Error"
    exc_msg = ""
    err_line_idx = -1

    for idx, line in enumerate(lines):
        m = err_pattern.match(line.strip())
        if m:
            exc_type = m.group(1)
            exc_msg = m.group(2).strip()
            err_line_idx = idx
            break

    # If no standard Error: pattern found, check for SyntaxError pointer above error
    if err_line_idx == -1:
        for idx, line in enumerate(lines):
            if "Error:" in line:
                parts = line.split("Error:", 1)
                exc_type = parts[0].strip().split()[-1] + "Error"
                exc_msg = parts[1].strip()
                err_line_idx = idx
                break

    if err_line_idx == -1:
        # Fallback if stderr contains node stack trace without top header
        if any("    at " in l for l in lines):
            exc_type = "NodeExecutionError"
            exc_msg = lines[0].strip()
        else:
            return None

    # 2. Parse frames: "    at Object.handler (/app/src/index.js:10:5)"
    # or "    at /app/src/index.js:10:5"
    frame_pattern = re.compile(
        r'^\s*at\s+(?:(?P<func>[^(\n]+?)\s+\((?P<file1>[^:\n]+):(?P<line1>\d+):(?P<col1>\d+)\)|(?P<file2>[^:\n]+):(?P<line2>\d+):(?P<col2>\d+))'
    )

    frames: List[Dict[str, Any]] = []

    for line in lines[err_line_idx:]:
        fm = frame_pattern.match(line)
        if fm:
            func = fm.group("func")
            if func:
                func = func.strip()
                # Clean method prefixes like "async Object.handler" or "new Server"
                if func.startswith("async "):
                    func = func[6:].strip()
            else:
                func = "<anonymous>"

            filepath = fm.group("file1") or fm.group("file2")
            lineno_str = fm.group("line1") or fm.group("line2")
            colno_str = fm.group("col1") or fm.group("col2")

            lineno = int(lineno_str) if lineno_str else 1
            colno = int(colno_str) if colno_str else 1

            # Attempt to read code line from disk if file exists
            code_line = ""
            if filepath and os.path.isfile(filepath):
                try:
                    with open(filepath, "r", encoding="utf-8", errors="replace") as sf:
                        all_lines = sf.readlines()
                        if 1 <= lineno <= len(all_lines):
                            code_line = all_lines[lineno - 1].strip()
                except Exception:
                    pass

            frames.append({
                "function": func,
                "filename": filepath,
                "lineno": lineno,
                "colno": colno,
                "code_line": code_line,
                "locals": {}
            })

    if not frames:
        return None

    # 3. Resolve culprit root cause frame
    # Filter out node internal modules (node:internal/..., internal/...)
    user_frames = [
        f for f in frames
        if not f["filename"].startswith("node:")
        and "node_modules" not in f["filename"]
        and not f["filename"].startswith("internal/")
    ]
    culprit_frame = user_frames[0] if user_frames else frames[0]

    # Synthesize locals from error message heuristics (e.g. reading property 'foo')
    locals_dict: Dict[str, Any] = {}
    prop_match = re.search(r"reading ['\"]([a-zA-Z0-9_$]+)['\"]", exc_msg)
    if prop_match:
        locals_dict[prop_match.group(1)] = None
    not_defined_match = re.search(r"([a-zA-Z0-9_$]+) is not defined", exc_msg)
    if not_defined_match:
        locals_dict[not_defined_match.group(1)] = "<undefined>"

    culprit_frame["locals"] = locals_dict

    return {
        "exception_type": exc_type,
        "exception_message": exc_msg,
        "runtime": "node",
        "root_cause": culprit_frame,
        "frames": frames,
        "raw_stderr": stderr_text
    }
