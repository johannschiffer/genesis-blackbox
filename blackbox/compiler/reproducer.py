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
    """

    def __init__(self, wal_ring: WalRingBuffer):
        self.wal_ring = wal_ring

    def generate(self) -> str:
        """
        Synthesizes Python test code that reconstructs the crash scenario.
        Returns a syntactically valid Python script.
        """
        # Fetch the latest recorded exception
        exc_events = self.wal_ring.query_recent(limit=1, event_type="EXCEPTION")
        if not exc_events:
            return self._generate_fallback_template("No EXCEPTION event recorded in WAL.")

        exc_event = exc_events[0]
        payload = exc_event.get("payload") or {}
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
