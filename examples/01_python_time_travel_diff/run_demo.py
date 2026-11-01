#!/usr/bin/env python3
"""
Genesis Blackbox v0.2.0 — Showcase 1
Python Time-Travel SQL & State Divergence Runner

Executes two consecutive runs of an order pipeline:
- Run 1 (Healthy Baseline): Valid batch, records HTTP auth + SQL queries.
- Run 2 (Failing Run): Parameter mutation (discount -> None) triggering constraint crash.
Automatically compiles:
1. .blackbox.md (Causal Crash Compiler)
2. .blackbox-diff.md (Time-Travel State Differ)
Pinpoints the exact divergence point in execution history.
"""

import os
import sys
import shutil

# Ensure blackbox is importable
repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from blackbox.recorder.wal_ring import WalRingBuffer
from blackbox.recorder.runner import run_target
from blackbox.ast_engine.graph import CodebaseGraph
from blackbox.compiler.autopsy import CausalCrashCompiler
from blackbox.compiler.state_differ import StateDiffer


def main() -> int:
    demo_dir = os.path.dirname(os.path.abspath(__file__))
    wal_dir = os.path.join(demo_dir, ".blackbox")
    wal_path = os.path.join(wal_dir, "wal.sqlite")
    autopsy_path = os.path.join(demo_dir, ".blackbox.md")
    diff_path = os.path.join(demo_dir, ".blackbox-diff.md")
    pipeline_script = os.path.join(demo_dir, "pipeline.py")

    # Clean previous run state
    if os.path.exists(wal_dir):
        shutil.rmtree(wal_dir, ignore_errors=True)
    for p in (autopsy_path, diff_path):
        if os.path.isfile(p):
            os.remove(p)

    os.makedirs(wal_dir, exist_ok=True)

    print("=" * 78)
    print("  GENESIS BLACKBOX SHOWCASE 1: PYTHON TIME-TRAVEL SQL & STATE DIVERGENCE")
    print("=" * 78)

    # -------------------------------------------------------------------------
    # RUN 1: Healthy Baseline Execution
    # -------------------------------------------------------------------------
    print("\n[Phase 1] Executing Run 1 (Healthy Baseline)...")
    rc1 = run_target([pipeline_script, "--mode", "baseline", "--db", wal_path], db_path=wal_path)
    print(f"  -> Run 1 Exit Code: {rc1} (Status: PASSING)")

    # -------------------------------------------------------------------------
    # RUN 2: Failing Run Execution (Silent parameter mutation)
    # -------------------------------------------------------------------------
    print("\n[Phase 2] Executing Run 2 (Failing Run: Upstream Parameter Mutation)...")
    crashed = False
    caught_exc = None
    try:
        run_target([pipeline_script, "--mode", "failing", "--db", wal_path], db_path=wal_path)
    except Exception as exc:
        crashed = True
        caught_exc = exc
        print(f"  -> Run 2 Crashed as expected with: {type(exc).__name__}: {exc}")

    if not crashed:
        print("  -> ERROR: Run 2 was expected to raise an exception but exited cleanly.")
        return 1

    # -------------------------------------------------------------------------
    # PHASE 3: Compile Causal Crash Autopsy (.blackbox.md)
    # -------------------------------------------------------------------------
    print("\n[Phase 3] Invoking CausalCrashCompiler...")
    with WalRingBuffer(db_path=wal_path) as ring:
        graph = CodebaseGraph()
        graph.parse_file(pipeline_script)
        compiler = CausalCrashCompiler(wal_ring=ring, ast_graph=graph)
        autopsy_md = compiler.compile()

    with open(autopsy_path, "w", encoding="utf-8") as f:
        f.write(autopsy_md)
    print(f"  -> Autopsy Capsule generated: {autopsy_path} ({len(autopsy_md)} bytes)")

    # -------------------------------------------------------------------------
    # PHASE 4: Invoke Time-Travel StateDiffer (.blackbox-diff.md)
    # -------------------------------------------------------------------------
    print("\n[Phase 4] Invoking StateDiffer to isolate baseline vs failing divergence...")
    differ = StateDiffer(db_path=wal_path)
    try:
        diff_res = differ.diff()
        diff_md = differ.compile_markdown(diff_res)
    finally:
        differ.close()

    with open(diff_path, "w", encoding="utf-8") as f:
        f.write(diff_md)
    print(f"  -> State Diff generated: {diff_path} ({len(diff_md)} bytes)")

    # -------------------------------------------------------------------------
    # PHASE 5: Telemetry Summary & Pinpointed Divergence
    # -------------------------------------------------------------------------
    print("\n" + "-" * 78)
    print("  STRUCTURED TELEMETRY & PINPOINTED DIVERGENCE REPORT")
    print("-" * 78)
    b_run = diff_res.get("baseline_run", {})
    f_run = diff_res.get("failing_run", {})
    div = diff_res.get("divergence_point", {})
    var_diff = diff_res.get("variable_diff", {})

    print(f"Baseline Run ID : {b_run.get('run_id')} | Status: {b_run.get('status')} | Events: {b_run.get('event_count')}")
    print(f"Failing Run ID  : {f_run.get('run_id')} | Status: {f_run.get('status')} | Events: {f_run.get('event_count')}")
    print(f"Divergence Step : Step #{div.get('step')} ({div.get('type')})")
    print(f"Description     : {div.get('description')}")

    if div.get("failing_event"):
        fe = div["failing_event"]
        fp = fe.get("payload", {})
        print(f"Failing Query   : {fp.get('query')}")
        print(f"Bound Params    : {fp.get('params')}")
        print(f"Database Error  : {fp.get('error')}")

    mutated = var_diff.get("mutated_variables", {})
    if mutated:
        print("Mutated Variables:")
        for var_name, details in mutated.items():
            print(f"  - {var_name}: {details.get('issue')} (baseline={details.get('baseline')}, failing={details.get('failing')})")

    print("-" * 78)
    print(f"Artifacts successfully produced:\n  - {autopsy_path}\n  - {diff_path}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())

