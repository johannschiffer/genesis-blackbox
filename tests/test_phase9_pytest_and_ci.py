"""
Genesis Blackbox: Phase 9 Unit Tests
Validates the native Pytest plugin, automatic failure autopsy capture,
crash reproducer generation, GitHub Actions Step Summary compiler, and CLI commands.
"""

import os
import sys
import json
import sqlite3
import subprocess
import pytest
from pathlib import Path

from blackbox.ci.github_summary import export_github_step_summary
from blackbox.cli import main as cli_main
from blackbox.recorder.wal_ring import WalRingBuffer


def test_pytest_plugin_successful_test_recording(tmp_path: Path):
    """Verifies that passing tests with --blackbox record TEST_START and TEST_END events in the WAL."""
    db_file = tmp_path / "test_wal.sqlite"
    test_file = tmp_path / "test_sample_pass.py"
    test_file.write_text(
        "def test_math_ok():\n"
        "    '''Test math operations.'''\n"
        "    x = 40 + 2\n"
        "    assert x == 42\n"
    )

    cmd = [
        sys.executable,
        "-m",
        "pytest",
        str(test_file),
        "-p", "blackbox",
        "--blackbox",
        f"--blackbox-db={db_file}",
        "-v"
    ]


    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(tmp_path))
    assert result.returncode == 0, f"Pytest failed: {result.stdout}\n{result.stderr}"

    # Verify events in WAL
    wal = WalRingBuffer(db_path=str(db_file))
    events = wal.query_recent(limit=10)
    event_types = [e["event_type"] for e in events]

    assert "TEST_START" in event_types
    assert "TEST_END" in event_types

    start_event = next(e for e in events if e["event_type"] == "TEST_START")
    assert "test_sample_pass.py::test_math_ok" in start_event["payload"]["nodeid"]
    wal.close()


def test_pytest_plugin_failing_test_generates_autopsy_and_reproducer(tmp_path: Path):
    """Verifies that a failing test captures stack frames, emits .blackbox.md, and synthesizes reproducer script."""
    db_file = tmp_path / "test_fail_wal.sqlite"
    test_file = tmp_path / "test_sample_fail.py"
    test_file.write_text(
        "def test_divide_by_zero():\n"
        "    divisor = 0\n"
        "    dividend = 100\n"
        "    return dividend / divisor\n"
    )

    cmd = [
        sys.executable,
        "-m",
        "pytest",
        str(test_file),
        "-p", "blackbox",
        "--blackbox",
        f"--blackbox-db={db_file}",
        "-v"

    ]

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(tmp_path))
    # Pytest should return exit code 1 due to the intentional failure
    assert result.returncode == 1

    # Check terminal banner output
    assert "[GENESIS-BLACKBOX] Test Failure Captured" in result.stdout or "[GENESIS-BLACKBOX] Test Failure Captured" in result.stderr

    # Check .blackbox.md created in working directory
    autopsy_file = tmp_path / ".blackbox.md"
    assert autopsy_file.exists()
    autopsy_content = autopsy_file.read_text(encoding="utf-8")
    assert "ZeroDivisionError" in autopsy_content
    assert "dividend" in autopsy_content or "divisor" in autopsy_content

    # Check test_blackbox_reproduce.py created
    reproduce_file = tmp_path / "test_blackbox_reproduce.py"
    assert reproduce_file.exists()
    reproduce_code = reproduce_file.read_text(encoding="utf-8")
    assert "def test_reproduce" in reproduce_code


    # Check WAL contains EXCEPTION event
    wal = WalRingBuffer(db_path=str(db_file))
    exceptions = wal.query_recent(limit=5, event_type="EXCEPTION")
    assert len(exceptions) >= 1
    assert exceptions[0]["payload"]["exception_type"] == "ZeroDivisionError"
    wal.close()


def test_github_step_summary_formatting_and_export(tmp_path: Path):
    """Verifies GitHub Actions step summary generation with collapsible blocks and environment export."""
    autopsy_path = tmp_path / ".blackbox.md"
    autopsy_path.write_text(
        "# BLACKBOX CAUSAL AUTOPSY: ZeroDivisionError\n\n"
        "## 1. Crash Overview\n"
        "- Crash Timestamp: 2026-10-27T21:00:00Z\n"
        "- Exception Type: `ZeroDivisionError`\n"
        "- Exception Message: division by zero\n\n"
        "## 2. Temporal Causal Context (Pre-Crash Flight Events)\n"
        "| ID | Type | Source | Latency |\n"
        "|---|---|---|---|\n"
        "| #1 | HTTP_REQ | api | 1.2ms |\n\n"
        "## 3. Inspected Call Stack & Local Variable Scopes\n"
        "### Frame #1: `divide` in `calc.py:10`\n"
        "- `divisor`: 0\n\n"
        "## 4. AST Blast Radius & Dependency Impact\n"
        "- Upstream Callers: `run_calc`\n\n"
        "## 6. Suggested Fix Mandate\n"
        "1. Guard against divisor == 0.\n",
        encoding="utf-8"
    )

    summary_file = tmp_path / "step_summary.md"
    output_md = export_github_step_summary(autopsy_path=str(autopsy_path), summary_file=str(summary_file))

    assert summary_file.exists()
    saved_content = summary_file.read_text(encoding="utf-8")
    assert "<details open>" in saved_content
    assert "<summary><b>⏱️ Temporal Causal Context" in saved_content
    assert "<summary><b>🔍 Inspected Call Stack" in saved_content
    assert "ZeroDivisionError" in saved_content
    assert "Guard against divisor == 0" in saved_content
    assert output_md == saved_content.strip() or output_md in saved_content


def test_github_step_summary_missing_autopsy_fallback(tmp_path: Path):
    """Verifies graceful fallback summary when no autopsy file is present."""
    non_existent = str(tmp_path / "non_existent.md")
    summary = export_github_step_summary(autopsy_path=non_existent)

    assert "No failure autopsy capsule was located" in summary
    assert "[!NOTE]" in summary


def test_cli_ci_summary_subcommand(tmp_path: Path, monkeypatch, capsys):
    """Verifies CLI execution of 'blackbox ci-summary' writing to a file and stdout."""
    autopsy_path = tmp_path / "sample.blackbox.md"
    autopsy_path.write_text(
        "# BLACKBOX CAUSAL AUTOPSY: ValueError\n\n"
        "## 1. Crash Overview\n"
        "- Exception Type: `ValueError`\n"
        "- Exception Message: invalid parameter\n",
        encoding="utf-8"
    )

    out_file = tmp_path / "summary_out.md"
    exit_code = cli_main(["ci-summary", "--autopsy", str(autopsy_path), "--output", str(out_file)])
    assert exit_code == 0
    assert out_file.exists()
    content = out_file.read_text(encoding="utf-8")
    assert "ValueError" in content
    assert "Genesis Blackbox" in content

    # Test stdout output when neither output nor env var is defined
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    exit_code_stdout = cli_main(["ci-summary", "--autopsy", str(autopsy_path)])
    assert exit_code_stdout == 0
    captured = capsys.readouterr()
    assert "ValueError" in captured.out
