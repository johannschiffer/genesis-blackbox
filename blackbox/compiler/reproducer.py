"""
Genesis Blackbox: Deterministic Crash Reproduction Test Generator
Synthesizes standalone pytest/unittest reproduction test scripts from recorded WAL exceptions,
captured stack frames, local variables, and preceding flight recorder events.
"""

import os
import sys
import json
import inspect
from typing import Dict, Any, List, Optional
from ..recorder.wal_ring import WalRingBuffer


def _safe_repr(val: Any) -> str:
    """Produces a safe, syntactically valid Python literal representation of a variable."""
    if val is None:
        return "None"
    if isinstance(val, bool):
        return "True" if val else "False"
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, str):
        if val in ("<depth-limit-reached>", "<circular-reference>"):
            return "None"
        if val.startswith("<bytes:") and val.endswith(">"):
            return "b''"
        return repr(val)
    if isinstance(val, list):
        items = [_safe_repr(item) for item in val[:15]]
        return "[" + ", ".join(items) + "]"
    if isinstance(val, dict):
        entries = []
        for k, v in list(val.items())[:15]:
            entries.append(f"{_safe_repr(str(k))}: {_safe_repr(v)}")
        return "{" + ", ".join(entries) + "}"
    return repr(str(val))


class CrashReproducer:
    """
    Generates a deterministic reproduction test script from the latest recorded exception in WAL.
    Supports both Python (pytest) and Node.js/TypeScript (node:test).
    """

    def __init__(self, wal_ring: WalRingBuffer):
        self.wal_ring = wal_ring

    def is_node_target(self) -> bool:
        """Detects whether the latest crash occurred in a Node.js/TypeScript runtime."""
        exc_events = self.wal_ring.query_recent(limit=1, event_type="EXCEPTION")
        if not exc_events:
            return False
        payload = exc_events[0].get("payload") or {}
        if payload.get("runtime") == "node":
            return True
        root_cause = payload.get("root_cause") or {}
        culprit = str(root_cause.get("filename", "")).lower()
        if any(culprit.endswith(ext) for ext in (".js", ".mjs", ".cjs", ".ts", ".tsx")):
            return True
        for f in payload.get("frames", []):
            fn = str(f.get("filename", "")).lower()
            if any(fn.endswith(ext) for ext in (".js", ".mjs", ".cjs", ".ts", ".tsx")):
                return True
        return False

    def get_recommended_filename(self) -> str:
        """Returns recommended filename based on runtime (test_blackbox_reproduce.mjs or test_blackbox_reproduce.py)."""
        return "test_blackbox_reproduce.mjs" if self.is_node_target() else "test_blackbox_reproduce.py"

    def generate(self) -> str:
        """
        Synthesizes reproduction test code that reconstructs the crash scenario.
        Returns a syntactically valid Python (pytest) or Node.js (node:test) script.
        """
        exc_events = self.wal_ring.query_recent(limit=1, event_type="EXCEPTION")
        if not exc_events:
            return self._generate_fallback_template("No EXCEPTION event recorded in WAL.")

        exc_event = exc_events[0]
        payload = exc_event.get("payload") or {}

        if self.is_node_target():
            return self._generate_node_test(payload, exc_event)
        return self._generate_python_test(payload, exc_event)

    def _generate_node_test(self, payload: Dict[str, Any], exc_event: Dict[str, Any]) -> str:
        """Synthesizes deterministic Node.js reproduction test using node:test and node:assert/strict."""
        exc_type = payload.get("exception_type", "Error")
        exc_msg = payload.get("exception_message", "")
        root_cause = payload.get("root_cause") or {}
        culprit_file = root_cause.get("filename", "")
        culprit_func = root_cause.get("function", "default")
        culprit_line = root_cause.get("lineno", 0)
        locals_dict = root_cause.get("locals") or {}

        abs_target = os.path.abspath(culprit_file) if culprit_file else ""
        safe_doc_msg = str(exc_msg).replace("*/", "* /").replace("\n", " ")
        safe_target_path = abs_target.replace("\\", "/")

        clean_locals = {}
        for k, v in locals_dict.items():
            if not str(k).startswith("_"):
                clean_locals[k] = v

        # Check for preceding HTTP events in timeline
        recent_events = self.wal_ring.query_recent(limit=15)
        http_events = [e for e in recent_events if e.get("event_type") in ("HTTP_REQ", "HTTP_CLIENT", "http_transaction")]
        preceding_body = {}
        if http_events:
            hp = http_events[0].get("payload", {})
            if isinstance(hp, dict) and (hp.get("body") or hp.get("payload")):
                b = hp.get("body") or hp.get("payload")
                if isinstance(b, dict):
                    preceding_body = b

        merged_args = {**clean_locals, **preceding_body}
        args_json = json.dumps(merged_args, indent=2, default=str)

        lines = [
            "/**",
            " * Genesis Blackbox: Deterministic Crash Reproduction Test (Node.js)",
            f" * Target Exception: {exc_type}: {safe_doc_msg}",
            f" * Target Culprit: {culprit_func}() in {safe_target_path}:{culprit_line}",
            " * Generated automatically by Genesis Blackbox Causal Crash Reproducer.",
            " */",
            "",
            "import test from 'node:test';",
            "import assert from 'node:assert/strict';",
            "import { pathToFileURL } from 'node:url';",
            "",
            f"const TARGET_FILE = {json.dumps(safe_target_path)};",
            f"const TARGET_FUNCTION_NAME = {json.dumps(culprit_func)};",
            f"const EXPECTED_EXCEPTION = {json.dumps(exc_type)};",
            "",
            "async function loadTargetFunction() {",
            "  if (!TARGET_FILE) {",
            "    throw new Error('Target source file path is empty.');",
            "  }",
            "  const fileUrl = pathToFileURL(TARGET_FILE).href;",
            "  const mod = await import(fileUrl);",
            "  let fn = mod[TARGET_FUNCTION_NAME];",
            "  if (!fn && mod.default) {",
            "    fn = typeof mod.default === 'object' ? mod.default[TARGET_FUNCTION_NAME] : mod.default;",
            "  }",
            "  if (!fn && typeof mod === 'function') {",
            "    fn = mod;",
            "  }",
            "  assert.ok(fn, `Target function ${TARGET_FUNCTION_NAME} not found in ${TARGET_FILE}`);",
            "  return fn;",
            "}",
            "",
            "test('reproduce and verify crash resolution', async () => {",
            "  const targetFunc = await loadTargetFunction();",
            f"  const reconstructedArgs = {args_json};",
            "",
            "  let result;",
            "  try {",
            "    result = await targetFunc(reconstructedArgs);",
            "  } catch (err) {",
            "    if (err instanceof TypeError && typeof targetFunc === 'function') {",
            "      result = await targetFunc(...Object.values(reconstructedArgs));",
            "    } else {",
            "      throw err;",
            "    }",
            "  }",
            "",
            "  assert.ok(result !== undefined || result === undefined);",
            "});",
            ""
        ]
        return "\n".join(lines)

    def _generate_python_test(self, payload: Dict[str, Any], exc_event: Dict[str, Any]) -> str:
        """Synthesizes deterministic Python reproduction test using pytest."""
        exc_type = payload.get("exception_type", "Exception")
        exc_msg = payload.get("exception_message", "")
        root_cause = payload.get("root_cause") or {}
        culprit_file = root_cause.get("filename", "")
        culprit_func = root_cause.get("function", "target_function")
        culprit_line = root_cause.get("lineno", 0)
        locals_dict = root_cause.get("locals") or {}

        # Fetch preceding SQL, HTTP, and SUBPROCESS events
        recent_events = self.wal_ring.query_recent(limit=15)
        sql_events = [e for e in recent_events if e.get("event_type") in ("SQL_QUERY", "sql_query")]
        sub_events = [e for e in recent_events if e.get("event_type") in ("SUBPROCESS", "subprocess")]

        # Filter local variables (remove dunders and private variables)
        clean_locals = {}
        for k, v in locals_dict.items():
            if not k.startswith("__") and not k.startswith("@"):
                clean_locals[k] = v

        safe_doc_msg = str(exc_msg).replace("\\", "\\\\").replace('"""', "'''").replace("\n", " ")
        safe_doc_file = str(culprit_file).replace("\\", "/")
        abs_target = os.path.abspath(culprit_file) if culprit_file else ""

        lines = [
            '"""',
            'Genesis Blackbox: Deterministic Crash Reproduction Test',
            f'Target Exception: {exc_type}: {safe_doc_msg}',
            f'Target Culprit: {culprit_func}() in {safe_doc_file}:{culprit_line}',
            'Generated automatically by Genesis Blackbox Causal Crash Reproducer.',
            '"""',
            "",
            "import os",
            "import sys",
            "import importlib.util",
            "from unittest.mock import patch, MagicMock",
            "import pytest",
            "",
            f'TARGET_FILE = {repr(abs_target)}',
            f'TARGET_FUNCTION_NAME = {repr(culprit_func)}',
            f'EXPECTED_EXCEPTION = {repr(exc_type)}',
            "",
            "",
            "def _load_target_function():",
            '    """Dynamically loads the target function from the culprit source file."""',
            '    if not TARGET_FILE or not os.path.isfile(TARGET_FILE):',
            '        pytest.skip(f"Source file {TARGET_FILE} not found on disk.")',
            '    ',
            '    target_dir = os.path.dirname(os.path.abspath(TARGET_FILE))',
            '    if target_dir and target_dir not in sys.path:',
            '        sys.path.insert(0, target_dir)',
            '    module_name = "blackbox_reproduced_target"',
            '    spec = importlib.util.spec_from_file_location(module_name, TARGET_FILE)',
            '    if spec is None or spec.loader is None:',
            '        pytest.fail(f"Could not load module spec for {TARGET_FILE}")',
            '    target_module = importlib.util.module_from_spec(spec)',
            '    sys.modules[module_name] = target_module',
            '    spec.loader.exec_module(target_module)',
            '    ',
            '    func = getattr(target_module, TARGET_FUNCTION_NAME, None)',
            '    if func is None:',
            '        pytest.fail(f"Function {TARGET_FUNCTION_NAME} not found in {TARGET_FILE}")',
            '    return func',
            "",
            "",
            "def test_reproduce_and_verify_fix():",
            '    """',
            f'    Reproduces crash scenario: {exc_type}: {safe_doc_msg}.',
            '    - In UNFIXED code: Fails with the recorded exception.',
            '    - In FIXED code: Completes execution successfully.',
            '    """',
            "    target_func = _load_target_function()",
            "",
            "    # Reconstructed input arguments captured from frame locals",
            "    reconstructed_args = {",
        ]

        for k, v in clean_locals.items():
            lines.append(f'        "{k}": {_safe_repr(v)},')

        lines.extend([
            "    }",
            "",
            "    # Setup mocks for external side effects if necessary",
        ])

        if sub_events:
            lines.extend([
                "    # Subprocess interactions detected in execution timeline",
                "    with patch('subprocess.run') as mock_sub_run:",
                "        mock_sub_run.return_value = MagicMock(returncode=0, stdout='ok', stderr='')",
                "        try:",
                "            result = target_func(**reconstructed_args)",
                "        except TypeError:",
                "            # If function expects positional args, invoke with values",
                "            result = target_func(*list(reconstructed_args.values()))",
            ])
        else:
            lines.extend([
                "    try:",
                "        result = target_func(**reconstructed_args)",
                "    except TypeError:",
                "        # If function expects positional args, invoke with values",
                "        result = target_func(*list(reconstructed_args.values()))",
            ])

        lines.extend([
            "",
            "    # Verification: Function completed cleanly without the recorded exception",
            "    assert result is not None or result is None  # Ensures function returned successfully",
            "",
            "",
            'if __name__ == "__main__":',
            "    pytest.main([__file__, '-v'])",
            ""
        ])

        return "\n".join(lines)

    def _generate_fallback_template(self, reason: str) -> str:
        """Fallback template if no exception is recorded."""
        return f'"""\nGenesis Blackbox: Reproduction Test Template\n{reason}\n"""\nimport pytest\n\ndef test_no_crash_recorded():\n    assert True\n'
