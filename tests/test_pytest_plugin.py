"""
Genesis Blackbox: Dedicated Pytest Plugin Unit Tests
Validates the modular blackbox.integrations.pytest_plugin hook,
assert failure autopsy generation, local variable capture,
custom output path resolution, and zero-overhead passing test verification.
"""

from pathlib import Path
import subprocess
import sys
import pytest

from blackbox.recorder.wal_ring import WalRingBuffer


def test_pytest_plugin_passing_test_zero_artifacts(tmp_path: Path):
    """Verifies that passing tests with --blackbox create no crash artifacts."""
    db_file = tmp_path / "passing_wal.sqlite"
    test_file = tmp_path / "test_ok.py"
    test_file.write_text(
        "def test_simple_pass():\n"
        "    a = 10\n"
        "    b = 20\n"
        "    assert a + b == 30\n"
    )

    cmd = [
        sys.executable,
        "-m",
        "pytest",
        str(test_file),
        "-p", "blackbox.integrations.pytest_plugin",
        "--blackbox",
        f"--blackbox-db={db_file}",
        "-v"
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(tmp_path))
    assert result.returncode == 0, f"Pytest failed: {result.stdout}\n{result.stderr}"

    # Verify no autopsy crash capsule created
    default_capsule = tmp_path / ".blackbox.md"
    assert not default_capsule.exists(), "Autopsy capsule should NOT be created for passing tests"

    # Verify WAL logged clean lifecycle
    wal = WalRingBuffer(db_path=str(db_file))
    events = wal.query_recent(limit=10)
    types = [e["event_type"] for e in events]
    assert "TEST_START" in types
    assert "TEST_END" in types
    assert "EXCEPTION" not in types
    wal.close()


def test_pytest_plugin_failing_assertion_generates_capsule_with_locals(tmp_path: Path):
    """Verifies that assertion failures generate .blackbox.md with exact traceback and locals."""
    db_file = tmp_path / "fail_wal.sqlite"
    test_file = tmp_path / "test_assertion_fail.py"
    test_file.write_text(
        "def test_business_logic_mismatch():\n"
        "    expected_release = 'genesis-v030-verified'\n"
        "    actual_release = 'genesis-v020-legacy'\n"
        "    user_counter = 1337\n"
        "    assert actual_release == expected_release\n"
    )

    cmd = [
        sys.executable,
        "-m",
        "pytest",
        str(test_file),
        "-p", "blackbox.integrations.pytest_plugin",
        "--blackbox",
        f"--blackbox-db={db_file}",
        "-v"
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(tmp_path))
    assert result.returncode == 1, "Pytest must exit with 1 on failed assertion"

    # Verify terminal output
    combined_out = result.stdout + result.stderr
    assert "[GENESIS-BLACKBOX] Test Failure Captured" in combined_out
    assert "test_assertion_fail.py::test_business_logic_mismatch" in combined_out

    # Verify .blackbox.md
    autopsy_file = tmp_path / ".blackbox.md"
    assert autopsy_file.exists(), ".blackbox.md capsule must be created"
    capsule_text = autopsy_file.read_text(encoding="utf-8")

    assert "AssertionError" in capsule_text
    assert "test_business_logic_mismatch" in capsule_text
    # Verify captured local variable scope
    assert "expected_release" in capsule_text or "genesis-v030-verified" in capsule_text
    assert "actual_release" in capsule_text or "genesis-v020-legacy" in capsule_text
    assert "1337" in capsule_text

    # Verify WAL recorded exception
    wal = WalRingBuffer(db_path=str(db_file))
    exc_events = wal.query_recent(limit=5, event_type="EXCEPTION")
    assert len(exc_events) >= 1
    assert exc_events[0]["payload"]["exception_type"] == "AssertionError"
    wal.close()


def test_pytest_plugin_custom_output_flag(tmp_path: Path):
    """Verifies --blackbox-output custom output path flag."""
    custom_output = tmp_path / "reports" / "custom_failure_capsule.md"
    test_file = tmp_path / "test_custom_fail.py"
    test_file.write_text(
        "def test_runtime_error():\n"
        "    marker_name = 'antigravity_custom_trace'\n"
        "    raise RuntimeError(f'Simulated failure: {marker_name}')\n"
    )

    cmd = [
        sys.executable,
        "-m",
        "pytest",
        str(test_file),
        "-p", "blackbox.integrations.pytest_plugin",
        "--blackbox",
        f"--blackbox-output={custom_output}",
        "-v"
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(tmp_path))
    assert result.returncode == 1

    # Verify custom path was used
    assert custom_output.exists(), f"Expected custom output at {custom_output}"
    assert not (tmp_path / ".blackbox.md").exists(), "Default .blackbox.md should not exist when custom path passed"

    content = custom_output.read_text(encoding="utf-8")
    assert "RuntimeError" in content
    assert "antigravity_custom_trace" in content
