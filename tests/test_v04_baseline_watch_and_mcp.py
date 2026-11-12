"""
Genesis Blackbox v0.4.0 Unit Tests
Validates:
1. Atomic promotion of wal.sqlite to wal.baseline.sqlite after clean execution (exit_code == 0).
2. Automatic baseline divergence (.blackbox-diff.md & ## Automatic Baseline Divergence in .blackbox.md)
   when a crashing run follows a clean baseline run.
3. SurgicalWatcher.run_once() file modification detection, surgical test selection, and deterministic execution.
4. CLI subcommand `blackbox watch --once`.
5. MCP tool `get_full_incident_context` delivering all 4 sections in 1 JSON-RPC call.
6. Updated agent rule templates preserving all existing directives and documenting v0.4.0 tools.
"""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from blackbox.cli import main as cli_main
from blackbox.mcp_server import BlackboxMCPServer
from blackbox.recorder.baseline import BaselineManager
from blackbox.recorder.runner import run_target
from blackbox.recorder.wal_ring import WalRingBuffer
from blackbox.recorder.watcher import SurgicalWatcher


def test_baseline_atomic_promotion_on_clean_run_and_skip_on_exception(tmp_path: Path):
    clean_script = tmp_path / "healthy_app.py"
    clean_script.write_text(
        "def run():\n"
        "    return 42\n"
        "if __name__ == '__main__':\n"
        "    assert run() == 42\n",
        encoding="utf-8",
    )

    wal_path = tmp_path / ".blackbox" / "wal.sqlite"
    baseline_path = tmp_path / ".blackbox" / "wal.baseline.sqlite"

    # Opt-out (--no-auto-baseline / auto_baseline=False) must NOT create baseline
    rc_no_auto = run_target([sys.executable, str(clean_script)], db_path=str(wal_path), auto_baseline=False)
    assert rc_no_auto == 0
    assert not baseline_path.exists()

    # Clean run with auto_baseline=True atomically promotes wal.sqlite -> wal.baseline.sqlite
    rc_ok = run_target([sys.executable, str(clean_script)], db_path=str(wal_path), auto_baseline=True)
    assert rc_ok == 0
    assert baseline_path.exists()

    # Verify cloned baseline is a valid SQLite DB with recorded lifecycle events and 0 exceptions
    conn = sqlite3.connect(str(baseline_path))
    try:
        rows = conn.execute("SELECT event_type FROM blackbox_events;").fetchall()
        types = [r[0] for r in rows]
        assert "SUBPROCESS_START" in types or "PROCESS_START" in types
        assert "EXCEPTION" not in types
    finally:
        conn.close()

    # Subsequent WAL containing an EXCEPTION must NOT overwrite baseline
    dirty_wal = tmp_path / "dirty_wal.sqlite"
    dirty_ring = WalRingBuffer(db_path=str(dirty_wal))
    dirty_ring.record("EXCEPTION", {"exception_type": "ZeroDivisionError", "exception_message": "div0"}, status="error")
    dirty_ring.close()
    assert BaselineManager.promote_to_baseline(wal_path=str(dirty_wal), baseline_path=str(baseline_path)) is False


def test_automatic_baseline_divergence_on_crashing_run(tmp_path: Path):
    app_file = tmp_path / "checkout.py"
    test_file = tmp_path / "test_checkout.py"
    wal_path = tmp_path / ".blackbox" / "wal.sqlite"
    baseline_path = tmp_path / ".blackbox" / "wal.baseline.sqlite"
    autopsy_path = tmp_path / ".blackbox.md"
    diff_path = tmp_path / ".blackbox-diff.md"

    # Pass 1: Healthy run via pytest --blackbox -> promotes baseline
    app_file.write_text(
        "def compute_total(amount, discount):\n"
        "    return amount - discount\n",
        encoding="utf-8",
    )
    test_file.write_text(
        "from checkout import compute_total\n"
        "def test_checkout_ok():\n"
        "    assert compute_total(100, 10) == 90\n",
        encoding="utf-8",
    )
    res_pass = subprocess.run(
        [
            sys.executable, "-m", "pytest", str(test_file),
            "-p", "blackbox.integrations.pytest_plugin",
            "--blackbox",
            f"--blackbox-db={wal_path}",
            f"--blackbox-output={autopsy_path}",
            "-v",
        ],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )
    assert res_pass.returncode == 0, res_pass.stdout + res_pass.stderr
    assert baseline_path.exists()
    assert not autopsy_path.exists()

    # Pass 2: Inject bug -> pytest --blackbox fails and automatically generates .blackbox-diff.md + section
    app_file.write_text(
        "def compute_total(amount, discount):\n"
        "    discount = None\n"
        "    return amount - discount\n",
        encoding="utf-8",
    )
    res_fail = subprocess.run(
        [
            sys.executable, "-m", "pytest", str(test_file),
            "-p", "blackbox.integrations.pytest_plugin",
            "--blackbox",
            f"--blackbox-db={wal_path}",
            f"--blackbox-output={autopsy_path}",
            "-v",
        ],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )
    assert res_fail.returncode == 1
    assert autopsy_path.exists()
    assert diff_path.exists()

    autopsy_text = autopsy_path.read_text(encoding="utf-8")
    diff_text = diff_path.read_text(encoding="utf-8")
    assert "## Automatic Baseline Divergence" in autopsy_text
    assert "BLACKBOX TIME-TRAVEL EXECUTION DIFF" in diff_text
    assert "TypeError" in autopsy_text


def test_surgical_watcher_run_once_selects_only_impacted_tests(tmp_path: Path):
    calc_file = tmp_path / "calculator.py"
    other_file = tmp_path / "unrelated.py"
    calc_test = tmp_path / "test_calculator.py"
    other_test = tmp_path / "test_unrelated.py"

    calc_file.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    other_file.write_text("def greet():\n    return 'hi'\n", encoding="utf-8")
    calc_test.write_text(
        "from calculator import add\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    other_test.write_text(
        "from unrelated import greet\n"
        "def test_greet():\n"
        "    assert greet() == 'hi'\n",
        encoding="utf-8",
    )

    watcher = SurgicalWatcher(root_dir=str(tmp_path), db_path=str(tmp_path / ".blackbox" / "wal.sqlite"))

    # No changes yet -> no execution
    idle = watcher.run_once()
    assert idle["changed_files"] == []
    assert idle["executed"] is False

    # Modify calculator.py (clean edit) -> only test_calculator.py runs and promotes baseline
    calc_file.write_text("def add(a, b):\n    # updated\n    return a + b\n", encoding="utf-8")
    os.utime(calc_file, None)

    pass_res = watcher.run_once()
    assert os.path.normpath(str(calc_file)) in pass_res["changed_files"]
    assert os.path.normpath(str(calc_test)) in pass_res["impacted_tests"]
    assert os.path.normpath(str(other_test)) not in pass_res["impacted_tests"]
    assert pass_res["executed"] is True
    assert pass_res["passed"] is True
    assert pass_res["baseline_promoted"] is True

    # Introduce regression in calculator.py -> watcher compiles .blackbox.md, .blackbox-diff.md, reproducer
    calc_file.write_text("def add(a, b):\n    raise ValueError('broken add')\n", encoding="utf-8")
    os.utime(calc_file, None)

    fail_res = watcher.run_once()
    assert fail_res["executed"] is True
    assert fail_res["passed"] is False
    assert (tmp_path / ".blackbox.md").exists()
    assert (tmp_path / ".blackbox-diff.md").exists()
    assert (tmp_path / "test_blackbox_reproduce.py").exists()


def test_cli_watch_once_subcommand(tmp_path: Path):
    src = tmp_path / "mod.py"
    src.write_text("def fn():\n    return 1\n", encoding="utf-8")

    rc = cli_main([
        "watch",
        "--root", str(tmp_path),
        "--interval", "0.1",
        "--once",
        "--db", str(tmp_path / ".blackbox" / "wal.sqlite"),
    ])
    assert rc == 0


def test_mcp_get_full_incident_context_single_call(tmp_path: Path):
    src_file = tmp_path / "billing.py"
    src_file.write_text(
        "def charge(amount):\n"
        "    if amount <= 0:\n"
        "        raise ValueError('amount must be positive')\n"
        "    return amount\n",
        encoding="utf-8",
    )
    test_file = tmp_path / "test_billing.py"
    test_file.write_text(
        "from billing import charge\n"
        "def test_charge():\n"
        "    assert charge(10) == 10\n",
        encoding="utf-8",
    )

    baseline_db = tmp_path / "wal.baseline.sqlite"
    b_ring = WalRingBuffer(db_path=str(baseline_db))
    b_ring.record("PROCESS_START", {"run_id": "base-1"}, source="test")
    b_ring.record("SQL_QUERY", {"query": "SELECT balance FROM accounts", "params": [1]}, source="sql")
    b_ring.record("PROCESS_EXIT", {"exit_code": 0}, source="test")
    b_ring.close()

    wal_db = tmp_path / "wal.sqlite"
    f_ring = WalRingBuffer(db_path=str(wal_db))
    f_ring.record("PROCESS_START", {"run_id": "fail-1"}, source="test")
    f_ring.record("SQL_QUERY", {"query": "SELECT balance FROM accounts", "params": [None]}, source="sql", status="error")
    f_ring.record(
        "EXCEPTION",
        {
            "exception_type": "ValueError",
            "exception_message": "amount must be positive",
            "traceback": "Traceback...\nValueError: amount must be positive\n",
            "root_cause": {
                "filename": str(src_file),
                "function": "charge",
                "lineno": 3,
                "locals": {"amount": -5},
            },
        },
        source="exception_hook",
        status="error",
    )
    f_ring.record("PROCESS_EXIT", {"exit_code": 1}, source="test", status="error")
    f_ring.close()

    server = BlackboxMCPServer(db_path=str(wal_db))
    try:
        # 1. With existing baseline_db_path
        resp = server.handle_request({
            "jsonrpc": "2.0",
            "id": 99,
            "method": "tools/call",
            "params": {
                "name": "get_full_incident_context",
                "arguments": {
                    "db_path": str(wal_db),
                    "baseline_db_path": str(baseline_db),
                    "root_dir": str(tmp_path),
                },
            },
        })
        assert "result" in resp
        payload = json.loads(resp["result"]["content"][0]["text"])
        assert set(payload.keys()) == {
            "autopsy_markdown",
            "baseline_diff",
            "reproduction_test",
            "impacted_tests",
        }
        assert "ValueError" in payload["autopsy_markdown"]
        assert isinstance(payload["baseline_diff"], dict)
        assert payload["baseline_diff"]["divergence_point"] is not None
        assert "def test_reproduce_and_verify_fix" in payload["reproduction_test"]
        assert any("test_billing.py" in p for p in payload["impacted_tests"])

        # 2. Without baseline_db_path -> baseline_diff must be None
        no_base_payload = server.get_full_incident_context(
            db_path=str(wal_db),
            baseline_db_path=str(tmp_path / "missing.baseline.sqlite"),
            root_dir=str(tmp_path),
        )
        assert no_base_payload["baseline_diff"] is None
    finally:
        server.close()


def test_agent_rules_templates_preserve_and_document_v040(tmp_path: Path):
    for kind, fname in [("cursor", ".cursorrules"), ("claude", "CLAUDE.md"), ("windsurf", ".windsurfrules")]:
        dest = tmp_path / fname
        rc = cli_main(["init-rules", "--type", kind, "--output", str(dest)])
        assert rc == 0
        text = dest.read_text(encoding="utf-8")
        # New v0.4.0 capabilities
        assert "get_full_incident_context" in text
        assert "pytest --blackbox" in text
        assert "blackbox watch" in text
        assert "wal.baseline.sqlite" in text
        assert "--no-auto-baseline" in text
        # Prior directives preserved
        assert "compare_execution_runs" in text
        assert "get_blast_radius" in text
        assert "get_repair_prompt" in text
        assert "test_candidate_patch" in text

