#!/usr/bin/env python3
"""
Genesis Blackbox v0.2.0 — Showcase 2
Node.js / TypeScript Runtime Preload & V8 Reproducer Runner

Demonstrates:
1. Zero-dependency Node.js runtime preload interceptor (`NODE_OPTIONS`) hooking fetch & unhandled rejections.
2. Automated credential and token redaction (Authorization, Cookie, ?api_key=).
3. Pure-Python polyglot AST blast-radius calculation across `.mjs` and `.ts` files.
4. Autonomous synthesis of a standalone `node:test` reproduction script (`test_blackbox_reproduce.mjs`).
"""

import os
import sys
import shutil
import subprocess

# Ensure blackbox is importable
repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from blackbox.recorder.wal_ring import WalRingBuffer
from blackbox.recorder.runner import run_target
from blackbox.ast_engine.graph import CodebaseGraph
from blackbox.compiler.reproducer import CrashReproducer
from blackbox.compiler.autopsy import CausalCrashCompiler


def main() -> int:
    demo_dir = os.path.dirname(os.path.abspath(__file__))
    wal_dir = os.path.join(demo_dir, ".blackbox")
    wal_path = os.path.join(wal_dir, "wal.sqlite")
    autopsy_path = os.path.join(demo_dir, ".blackbox.md")
    repro_path = os.path.join(demo_dir, "test_blackbox_reproduce.mjs")
    routes_ts = os.path.join(demo_dir, "routes.ts")
    app_mjs = os.path.join(demo_dir, "app.mjs")

    # Clean previous run state
    if os.path.exists(wal_dir):
        shutil.rmtree(wal_dir, ignore_errors=True)
    for p in (autopsy_path, repro_path):
        if os.path.isfile(p):
            os.remove(p)

    os.makedirs(wal_dir, exist_ok=True)

    print("=" * 78)
    print("  GENESIS BLACKBOX SHOWCASE 2: NODE.JS / TS RUNTIME PRELOAD & V8 REPRODUCER")
    print("=" * 78)

    # -------------------------------------------------------------------------
    # PHASE 1: Execute Node.js Application with Blackbox Preload Hook
    # -------------------------------------------------------------------------
    print("\n[Phase 1] Executing target: node app.mjs with Blackbox preload active...")
    orig_cwd = os.getcwd()
    os.chdir(demo_dir)
    try:
        ret_code = run_target(["node", "app.mjs"], db_path=wal_path)
    finally:
        os.chdir(orig_cwd)

    print(f"  -> Process exited with return code: {ret_code} (Expected failure: V8 crash captured)")

    # -------------------------------------------------------------------------
    # PHASE 2: Verify Token & Header Redaction in WAL Ring-Buffer
    # -------------------------------------------------------------------------
    print("\n[Phase 2] Inspecting recorded WAL events for automated credential redaction...")
    with WalRingBuffer(db_path=wal_path) as ring:
        http_events = ring.query_recent(limit=10, event_type="HTTP_CLIENT")
        exc_events = ring.query_recent(limit=10, event_type="EXCEPTION")

    if not http_events:
        print("  -> ERROR: No HTTP_CLIENT events intercepted in WAL database.")
        return 1

    fetch_ev = http_events[0]
    payload = fetch_ev.get("payload", {})
    url = payload.get("url", "")
    headers = payload.get("headers", {})

    print("  -> Intercepted Outbound Fetch Call:")
    print(f"     Sanitized URL    : {url}")
    print(f"     Authorization    : {headers.get('Authorization')}")
    print(f"     Cookie           : {headers.get('Cookie')}")
    print(f"     Preserved Header : X-Service-Name = '{headers.get('X-Service-Name')}'")

    assert "api_key=REDACTED" in url, "Expected api_key query param to be redacted"
    assert headers.get("Authorization") == "******** [REDACTED]", "Expected Authorization header redaction"
    assert headers.get("Cookie") == "******** [REDACTED]", "Expected Cookie header redaction"
    print("  -> Verification: ALL SENSITIVE CREDENTIALS SUCCESSFULLY REDACTED!")

    # -------------------------------------------------------------------------
    # PHASE 3: Polyglot AST Call Graph & Blast-Radius Calculation
    # -------------------------------------------------------------------------
    print("\n[Phase 3] Computing polyglot AST blast-radius across routes.ts & app.mjs...")
    graph = CodebaseGraph()
    graph.parse_file(routes_ts)
    graph.parse_file(app_mjs)

    radius = graph.get_blast_radius("processOrderRoute", file_path=routes_ts)
    print(f"  -> Target Symbol   : {radius.get('target')}")
    print(f"  -> Upstream Callers: {radius.get('upstream_callers')}")
    print(f"  -> Total Blast Rad : {radius.get('total_blast_radius')} dependent callers")

    assert "handleCheckout" in radius.get("upstream_callers", []), "Expected handleCheckout in callers"

    # -------------------------------------------------------------------------
    # PHASE 4: Autonomous node:test Reproduction Script Synthesis
    # -------------------------------------------------------------------------
    print("\n[Phase 4] Synthesizing standalone node:test reproduction script...")
    with WalRingBuffer(db_path=wal_path) as ring:
        reproducer = CrashReproducer(wal_ring=ring)
        assert reproducer.is_node_target() is True, "Target should be recognized as Node.js"
        repro_code = reproducer.generate()

        compiler = CausalCrashCompiler(wal_ring=ring, ast_graph=graph)
        autopsy_md = compiler.compile()

    with open(repro_path, "w", encoding="utf-8") as f:
        f.write(repro_code)
    with open(autopsy_path, "w", encoding="utf-8") as f:
        f.write(autopsy_md)

    print(f"  -> Synthesized {repro_path} ({len(repro_code)} bytes)")
    print(f"  -> Compiled {autopsy_path} ({len(autopsy_md)} bytes)")

    # Execute synthesized reproducer to prove failure reproduction
    print("\n[Phase 5] Executing synthesized reproduction test via 'node --test'...")
    proc_test = subprocess.run(
        ["node", "--test", os.path.basename(repro_path)],
        cwd=demo_dir,
        capture_output=True,
        text=True
    )
    print(f"  -> Reproducer returncode: {proc_test.returncode} (Expected 1: Reproducer successfully catches crash)")
    if proc_test.stderr or proc_test.stdout:
        first_line = (proc_test.stdout or proc_test.stderr).strip().splitlines()[0]
        print(f"  -> Reproducer output header: {first_line}")

    # -------------------------------------------------------------------------
    # TELEMETRY SUMMARY
    # -------------------------------------------------------------------------
    print("\n" + "-" * 78)
    print("  STRUCTURED TELEMETRY SUMMARY")
    print("-" * 78)
    if exc_events:
        ep = exc_events[0].get("payload", {})
        print(f"Runtime          : Node.js (V8) / TypeScript")
        print(f"Exception Type   : {ep.get('exception_type')}")
        print(f"Exception Message: {ep.get('exception_message')}")
        rc = ep.get("root_cause", {})
        print(f"Root Cause Func  : {rc.get('function')}() in {os.path.basename(rc.get('filename', ''))}:{rc.get('lineno')}")
        print(f"Culprit Line     : {rc.get('code_line')}")
    print(f"Tokens Redacted  : api_key=REDACTED, Bearer ******** [REDACTED]")
    print(f"AST Blast Radius : routes.ts:processOrderRoute -> app.mjs:handleCheckout")
    print(f"Reproduction     : {repro_path} (node:test)")
    print("-" * 78)
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())

