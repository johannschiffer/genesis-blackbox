"""
Genesis Blackbox v0.2.0 Test Suite
End-to-End Verification of Real-World Showcase Demonstrations & Benchmarks:
1. Showcase 1: Python Time-Travel SQL & State Divergence (examples/01_python_time_travel_diff/)
2. Showcase 2: Node.js/TypeScript Runtime Preload & V8 Reproducer (examples/02_node_ts_async_crash/)
3. Showcase 3: Autonomous 1-Shot Agent Repair Loop (examples/03_autonomous_patch_loop/)
"""

import os
import sys
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _clean_demo_artifacts(demo_dir: str, artifacts: list):
    """Safely cleans up generated runtime artifacts to leave zero untracked files."""
    for art in artifacts:
        target = os.path.join(demo_dir, art)
        if os.path.isdir(target):
            shutil.rmtree(target, ignore_errors=True)
        elif os.path.isfile(target):
            try:
                os.remove(target)
            except OSError:
                pass


def test_showcase_01_python_time_travel_diff():
    """Executes Showcase 1 demo end-to-end and validates artifacts & divergence report."""
    demo_dir = os.path.join(REPO_ROOT, "examples", "01_python_time_travel_diff")
    run_demo = os.path.join(demo_dir, "run_demo.py")
    autopsy_file = os.path.join(demo_dir, ".blackbox.md")
    diff_file = os.path.join(demo_dir, ".blackbox-diff.md")
    wal_dir = os.path.join(demo_dir, ".blackbox")

    _clean_demo_artifacts(demo_dir, [".blackbox.md", ".blackbox-diff.md", ".blackbox"])

    try:
        proc = subprocess.run(
            [sys.executable, run_demo],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30
        )

        assert proc.returncode == 0, f"Showcase 1 failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"

        # Verify stdout telemetry
        assert "SHOWCASE 1: PYTHON TIME-TRAVEL SQL & STATE DIVERGENCE" in proc.stdout
        assert "Run 1 Exit Code: 0 (Status: PASSING)" in proc.stdout
        assert "Run 2 Crashed as expected with: IntegrityError" in proc.stdout
        assert "Autopsy Capsule generated" in proc.stdout
        assert "State Diff generated" in proc.stdout
        assert "Divergence Step :" in proc.stdout
        assert "NOT NULL constraint failed: orders.discount" in proc.stdout

        # Verify generated markdown artifacts
        assert os.path.isfile(autopsy_file), ".blackbox.md artifact missing"
        assert os.path.isfile(diff_file), ".blackbox-diff.md artifact missing"

        with open(autopsy_file, "r", encoding="utf-8") as f:
            autopsy_content = f.read()
        assert "BLACKBOX CRASH AUTOPSY CAPSULE" in autopsy_content
        assert "IntegrityError" in autopsy_content

        with open(diff_file, "r", encoding="utf-8") as f:
            diff_content = f.read()
        assert "BLACKBOX TIME-TRAVEL EXECUTION DIFF" in diff_content
        assert "SQL Query Divergence" in diff_content
    finally:
        _clean_demo_artifacts(demo_dir, [".blackbox.md", ".blackbox-diff.md", ".blackbox"])


def test_showcase_02_node_ts_async_crash():
    """Executes Showcase 2 demo end-to-end and validates Node preload, redaction & AST blast radius."""
    demo_dir = os.path.join(REPO_ROOT, "examples", "02_node_ts_async_crash")
    run_demo = os.path.join(demo_dir, "run_demo.py")
    autopsy_file = os.path.join(demo_dir, ".blackbox.md")
    repro_file = os.path.join(demo_dir, "test_blackbox_reproduce.mjs")
    wal_dir = os.path.join(demo_dir, ".blackbox")

    _clean_demo_artifacts(demo_dir, [".blackbox.md", "test_blackbox_reproduce.mjs", ".blackbox"])

    try:
        proc = subprocess.run(
            [sys.executable, run_demo],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30
        )

        assert proc.returncode == 0, f"Showcase 2 failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"

        # Verify stdout telemetry
        assert "SHOWCASE 2: NODE.JS / TS RUNTIME PRELOAD & V8 REPRODUCER" in proc.stdout
        assert "Process exited with return code: 1" in proc.stdout
        assert "api_key=REDACTED" in proc.stdout
        assert "Authorization    : ******** [REDACTED]" in proc.stdout
        assert "ALL SENSITIVE CREDENTIALS SUCCESSFULLY REDACTED!" in proc.stdout
        assert "Target Symbol   : processOrderRoute" in proc.stdout
        assert "handleCheckout" in proc.stdout
        assert "Synthesized" in proc.stdout
        assert "test_blackbox_reproduce.mjs" in proc.stdout

        # Verify generated artifacts
        assert os.path.isfile(repro_file), "test_blackbox_reproduce.mjs missing"
        assert os.path.isfile(autopsy_file), ".blackbox.md missing"

        with open(repro_file, "r", encoding="utf-8") as f:
            repro_content = f.read()
        assert "node:test" in repro_content
        assert "processOrderRoute" in repro_content
        assert "TypeError" in repro_content

        with open(autopsy_file, "r", encoding="utf-8") as f:
            autopsy_content = f.read()
        assert "BLACKBOX CRASH AUTOPSY CAPSULE" in autopsy_content
        assert "routes.ts" in autopsy_content
    finally:
        _clean_demo_artifacts(demo_dir, [".blackbox.md", "test_blackbox_reproduce.mjs", ".blackbox"])


def test_showcase_03_autonomous_patch_loop():
    """Executes Showcase 3 demo end-to-end and validates 5-phase repair lifecycle & receipt."""
    demo_dir = os.path.join(REPO_ROOT, "examples", "03_autonomous_patch_loop")
    run_demo = os.path.join(demo_dir, "run_demo.py")
    receipt_file = os.path.join(demo_dir, ".blackbox-resolved.md")

    _clean_demo_artifacts(demo_dir, [".blackbox-resolved.md"])

    try:
        proc = subprocess.run(
            [sys.executable, run_demo],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=40
        )

        assert proc.returncode == 0, f"Showcase 3 failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"

        # Verify stdout telemetry
        assert "SHOWCASE 3: AUTONOMOUS 1-SHOT AGENT REPAIR LOOP" in proc.stdout
        assert "Target crashed as expected: ValueError: Invalid tax rate: -0.05" in proc.stdout
        assert "Pre-patch test execution: FAILED" in proc.stdout
        assert "Synthesized AI repair prompt packet" in proc.stdout
        assert "Patch Evaluation Success : True" in proc.stdout
        assert "Reproduction Test Passed : True" in proc.stdout
        assert "Rollback Safety Verified : Working tree 100% clean" in proc.stdout
        assert "CausalVerifier Resolved  : True" in proc.stdout
        assert "Receipt Generated" in proc.stdout

        # Verify generated receipt artifact
        assert os.path.isfile(receipt_file), ".blackbox-resolved.md missing"
        with open(receipt_file, "r", encoding="utf-8") as f:
            receipt_content = f.read()

        assert "BLACKBOX CRASH RESOLUTION VERIFICATION RECEIPT" in receipt_content
        assert "VERIFIED_RESOLVED (PASS)" in receipt_content
        assert "ValueError: Invalid tax rate: -0.05" in receipt_content
        assert "bb-proof-" in receipt_content
    finally:
        _clean_demo_artifacts(demo_dir, [".blackbox-resolved.md"])
