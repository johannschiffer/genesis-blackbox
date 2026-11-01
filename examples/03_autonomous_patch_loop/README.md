# Showcase 3: Autonomous 1-Shot Agent Repair Loop

Demonstrates the complete end-to-end autonomous agent repair loop in `genesis-blackbox` v0.2.0, proving candidate patch dry-running, guaranteed rollback safety, and cryptographic verification receipt emission.

---

## The 5-Phase Autonomous Lifecycle

```
[ Crashing Target ]
        │
        ▼  (1) run_target()
[ Flight Recorder WAL ]
        │
        ▼  (2) CrashReproducer.generate()
[ Standalone Reproduction Test ] ────► Proves Bug (FAILED)
        │
        ▼  (3) PromptSynthesizer.synthesize()
[ Context-Dense AI Repair Packet ]
        │
        ▼  (4) PatchValidator.evaluate_patch()
[ Sandboxed Evaluation & Rollback ] ──► Zero Working Tree Pollution
        │
        ▼  (5) CausalVerifier.verify()
[ .blackbox-resolved.md Receipt ] ────► Cryptographic Resolution Proof
```

1. **Record Crashing Target:**
   - Target script (`app.py -0.05`) crashes with an unhandled `ValueError: Invalid tax rate: -0.05`.
   - Flight recorder logs per-frame locals (`amount=100.0`, `rate=-0.05`).

2. **Autonomous Reproducer Synthesis:**
   - `CrashReproducer` automatically derives `test_blackbox_reproduce.py`.
   - Executing the test against unfixed code fails with the exact recorded exception, proving high-fidelity reproduction.

3. **Prompt Packet Synthesis:**
   - `PromptSynthesizer` compiles autopsy capsule, reproduction test, AST blast-radius, and execution context into an all-in-one prompt markdown (`prompt.md`).

4. **Candidate Patch Validation with Rollback Guarantee:**
   - An AI agent generates a candidate unified diff patch fixing the bug (`rate = 0.0`).
   - `PatchValidator.evaluate_patch()` tests `git apply --check`, AST syntax, and executes the reproduction test.
   - Proves **100% rollback safety**: once evaluated, the working tree is immediately and completely restored to the pre-patch state.

5. **Cryptographic Verification Receipt:**
   - After validation passes, the patch is applied.
   - `CausalVerifier` executes the reproduction test, calculates SHA-256 hashes of test code, test output, and modified files, and emits `.blackbox-resolved.md`.

---

## 1-Line Execution

```bash
python examples/03_autonomous_patch_loop/run_demo.py
```

Or using the CLI directly:

```bash
# 1. Record crashing target
blackbox record -- python app.py -0.05

# 2. Synthesize reproduction test
blackbox reproduce --output test_blackbox_reproduce.py

# 3. Synthesize repair prompt
blackbox pack --output prompt.md

# 4. Verify fix and emit receipt once patch is applied
blackbox verify --test test_blackbox_reproduce.py --receipt .blackbox-resolved.md
```

---

## Key Generated Artifacts

- `.blackbox-resolved.md`: Tamper-evident verification receipt signed with SHA-256 hashes, test execution time, and resolved status.

