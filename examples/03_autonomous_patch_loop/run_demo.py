#!/usr/bin/env python3
"""
Genesis Blackbox v0.2.0 — Showcase 3
Autonomous 1-Shot Agent Repair Loop Runner

Demonstrates the complete end-to-end autonomous repair lifecycle in a sandbox:
1. Record crashing target with Blackbox flight recorder (`run_target`).
2. Synthesize standalone reproduction test (`CrashReproducer`).
3. Generate AI repair prompt packet with polyglot AST blast-radius (`PromptSynthesizer`).
4. Validate candidate unified diff patch in sandboxed mode (`PatchValidator.evaluate_patch`), proving rollback safety.
5. Apply the fix and emit the cryptographic verification receipt (`CausalVerifier` -> `.blackbox-resolved.md`).
"""

import os
import sys
import shutil
import tempfile
import subprocess

# Ensure blackbox is importable
repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from blackbox.recorder.wal_ring import WalRingBuffer
from blackbox.recorder.runner import run_target
from blackbox.compiler.reproducer import CrashReproducer
from blackbox.compiler.prompt_synth import PromptSynthesizer
from blackbox.compiler.patch_validator import PatchValidator
from blackbox.compiler.verifier import CausalVerifier


def main() -> int:
    demo_dir = os.path.dirname(os.path.abspath(__file__))
    source_service = os.path.join(demo_dir, "tax_service.py")
    source_app = os.path.join(demo_dir, "app.py")
    receipt_dest = os.path.join(demo_dir, ".blackbox-resolved.md")

    # Clean existing receipt if present
    if os.path.isfile(receipt_dest):
        os.remove(receipt_dest)

    print("=" * 78)
    print("  GENESIS BLACKBOX SHOWCASE 3: AUTONOMOUS 1-SHOT AGENT REPAIR LOOP")
    print("=" * 78)

    # We perform the repair loop in an isolated git sandbox
    with tempfile.TemporaryDirectory(prefix="blackbox_sandbox_") as sandbox_dir:
        print(f"\n[Sandbox Setup] Initializing temporary git sandbox at:\n  {sandbox_dir}")
        subprocess.run(["git", "init"], cwd=sandbox_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "agent@genesis.local"], cwd=sandbox_dir, check=True)
        subprocess.run(["git", "config", "user.name", "Genesis Agent"], cwd=sandbox_dir, check=True)

        # Copy target files into sandbox
        shutil.copy(source_service, os.path.join(sandbox_dir, "tax_service.py"))
        shutil.copy(source_app, os.path.join(sandbox_dir, "app.py"))

        subprocess.run(["git", "add", "."], cwd=sandbox_dir, check=True)
        subprocess.run(["git", "commit", "-m", "Initial commit: checkout service with negative tax bug"], cwd=sandbox_dir, check=True)

        wal_path = os.path.join(sandbox_dir, ".blackbox", "wal.sqlite")
        os.makedirs(os.path.dirname(wal_path), exist_ok=True)

        # ---------------------------------------------------------------------
        # STEP 1: Record crashing target with Blackbox flight recorder
        # ---------------------------------------------------------------------
        print("\n[Step 1] Recording crashing target execution into flight recorder...")
        app_in_sandbox = os.path.join(sandbox_dir, "app.py")
        sys.modules.pop("tax_service", None)
        sys.path.insert(0, sandbox_dir)
        orig_cwd = os.getcwd()
        os.chdir(sandbox_dir)
        try:
            try:
                run_target([app_in_sandbox, "-0.05"], db_path=wal_path)
            except Exception as e:
                print(f"  -> Target crashed as expected: {type(e).__name__}: {e}")
        finally:
            os.chdir(orig_cwd)
            if sandbox_dir in sys.path:
                sys.path.remove(sandbox_dir)
            sys.modules.pop("tax_service", None)

        # ---------------------------------------------------------------------
        # STEP 2: Synthesize failing reproduction test
        # ---------------------------------------------------------------------
        print("\n[Step 2] Synthesizing standalone reproduction test with CrashReproducer...")
        test_file = os.path.join(sandbox_dir, "test_blackbox_reproduce.py")
        with WalRingBuffer(db_path=wal_path) as ring:
            reproducer = CrashReproducer(wal_ring=ring)
            repro_code = reproducer.generate()
            with open(test_file, "w", encoding="utf-8") as f:
                f.write(repro_code)

        print(f"  -> Generated {os.path.basename(test_file)} ({len(repro_code)} bytes)")

        # Verify reproducer catches the crash on unfixed code
        proc_pre = subprocess.run([sys.executable, "-m", "pytest", test_file, "-q"], cwd=sandbox_dir, capture_output=True, text=True)
        assert proc_pre.returncode != 0, "Reproduction test should FAIL before patch"
        print(f"  -> Pre-patch test execution: FAILED (Exit Code {proc_pre.returncode}) — Crash scenario confirmed!")

        # ---------------------------------------------------------------------
        # STEP 3: Generate AI repair prompt packet
        # ---------------------------------------------------------------------
        print("\n[Step 3] Generating AI repair prompt packet with PromptSynthesizer...")
        with WalRingBuffer(db_path=wal_path) as ring:
            synth = PromptSynthesizer(wal_ring=ring, repo_root=sandbox_dir, db_path=wal_path)
            prompt_packet = synth.synthesize()

        prompt_dest = os.path.join(sandbox_dir, "prompt.md")
        with open(prompt_dest, "w", encoding="utf-8") as f:
            f.write(prompt_packet)
        print(f"  -> Synthesized AI repair prompt packet ({len(prompt_packet)} bytes)")

        # ---------------------------------------------------------------------
        # STEP 4: Validate candidate unified diff patch in sandboxed mode
        # ---------------------------------------------------------------------
        print("\n[Step 4] Evaluating candidate AI patch via PatchValidator (Dry-Run & Rollback)...")
        # Candidate unified diff patch fixing the ValueError bug
        candidate_patch = (
            "diff --git a/tax_service.py b/tax_service.py\n"
            "--- a/tax_service.py\n"
            "+++ b/tax_service.py\n"
            "@@ -10,3 +10,3 @@\n"
            "     if rate < 0:\n"
            "-        raise ValueError(f\"Invalid tax rate: {rate}\")\n"
            "+        rate = 0.0\n"
            "     return amount * rate\n"
        )

        validator = PatchValidator(repo_root=sandbox_dir, db_path=wal_path)
        eval_result = validator.evaluate_patch(candidate_patch, test_path="test_blackbox_reproduce.py")

        print(f"  -> Patch Evaluation Success : {eval_result.get('success')}")
        print(f"  -> AST Syntax Valid         : {eval_result.get('syntax_valid')}")
        print(f"  -> Reproduction Test Passed : {eval_result.get('reproducer_passed')}")

        assert eval_result.get("success") is True, f"Patch evaluation failed: {eval_result.get('output')}"
        assert eval_result.get("reproducer_passed") is True, "Reproducer did not pass under candidate patch"

        # Verify rollback safety: workspace MUST remain in unfixed state after evaluate_patch
        with open(os.path.join(sandbox_dir, "tax_service.py"), "r", encoding="utf-8") as f:
            src_after_eval = f.read()

        assert "raise ValueError" in src_after_eval, "Expected rollback to preserve original source"
        assert "rate = 0.0" not in src_after_eval, "Candidate patch leaked into working tree"
        print("  -> Rollback Safety Verified : Working tree 100% clean and restored to original unfixed state!")

        # ---------------------------------------------------------------------
        # STEP 5: Apply verified patch & emit cryptographic resolution receipt
        # ---------------------------------------------------------------------
        print("\n[Step 5] Applying verified patch and issuing cryptographic verification receipt...")
        subprocess.run(["git", "apply", "-"], input=candidate_patch, text=True, cwd=sandbox_dir, check=True)

        receipt_file = os.path.join(sandbox_dir, ".blackbox-resolved.md")
        with WalRingBuffer(db_path=wal_path) as ring:
            verifier = CausalVerifier(wal_ring=ring, db_path=wal_path, receipt_path=receipt_file)
            ver_res = verifier.verify(test_path=test_file)

        print(f"  -> CausalVerifier Resolved  : {ver_res.get('resolved')}")
        print(f"  -> Test Duration            : {ver_res.get('duration_ms')}ms")
        print(f"  -> Receipt Generated        : {receipt_file} ({os.path.getsize(receipt_file)} bytes)")

        assert ver_res.get("resolved") is True, "CausalVerifier failed to resolve bug"
        assert os.path.isfile(receipt_file), "Expected .blackbox-resolved.md to exist"

        # Copy receipt to example directory for inspection
        shutil.copy(receipt_file, receipt_dest)

    # -------------------------------------------------------------------------
    # TELEMETRY & RECEIPT SUMMARY
    # -------------------------------------------------------------------------
    print("\n" + "-" * 78)
    print("  AUTONOMOUS 1-SHOT REPAIR TELEMETRY & VERIFICATION RECEIPT")
    print("-" * 78)
    with open(receipt_dest, "r", encoding="utf-8") as f:
        receipt_text = f.read()

    for line in receipt_text.strip().splitlines()[:12]:
        print(f"  {line}")

    print("-" * 78)
    print(f"Cryptographic resolution receipt saved to: {receipt_dest}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
