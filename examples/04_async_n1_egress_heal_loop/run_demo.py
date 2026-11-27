"""
Example 04: Async TraceContext, N+1 SQL, Offline Egress Mocker, ResourceProfiler & Heal Plan
Runs completely offline using an ephemeral local HTTPServer on 127.0.0.1:0.
"""

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from blackbox import (  # noqa: E402
    AutonomousHealPlanner,
    CausalCrashCompiler,
    CrashReproducer,
    EgressMocker,
    ExceptionHook,
    FlakeAnalyzer,
    ResourceProfiler,
    SqlInterceptor,
    TraceContext,
    WalRingBuffer,
)
from service import enrich_and_bill_order  # noqa: E402


class _PricingHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        body = json.dumps({"tax_multiplier": 1.21, "api_key": "should_be_redacted"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _seed_app_db(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE line_items (id INTEGER PRIMARY KEY, order_id INTEGER);")
    conn.execute("CREATE TABLE item_catalog (id INTEGER PRIMARY KEY, price REAL);")
    for idx in range(1, 8):
        conn.execute("INSERT INTO line_items (id, order_id) VALUES (?, 42);", (idx,))
        conn.execute("INSERT INTO item_catalog (id, price) VALUES (?, ?);", (idx, 10.0 * idx))
    conn.commit()
    conn.close()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="bb_ex04_") as tmpdir:
        wal_path = os.path.join(tmpdir, ".blackbox", "wal.sqlite")
        app_db = os.path.join(tmpdir, "orders.sqlite")
        _seed_app_db(app_db)

        server = HTTPServer(("127.0.0.1", 0), _PricingHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        base_url = f"http://127.0.0.1:{port}"
        ring = WalRingBuffer(db_path=wal_path)
        hook = ExceptionHook(wal_ring=ring)
        try:
            with EgressMocker(wal_ring=ring), ResourceProfiler().profile_scope(wal_ring=ring, label="checkout_async"):
                SqlInterceptor.install_sqlite3_global(ring)
                try:
                    with TraceContext(trace_id="trace-demo-04"):
                        try:
                            asyncio.run(
                                enrich_and_bill_order(
                                    order_id=42,
                                    base_url=base_url,
                                    api_key="sk-live-demo-999",
                                    db_path=app_db,
                                    discount_rate=None,
                                )
                            )
                        except Exception as exc:
                            hook.handle_exception(type(exc), exc, exc.__traceback__)
                finally:
                    SqlInterceptor.uninstall_sqlite3_global()

            autopsy = CausalCrashCompiler(wal_ring=ring).compile()
            assert "N+1" in autopsy, "Expected N+1 SQL diagnostics in .blackbox.md"
            assert "Resource & Memory Footprint" in autopsy, "Expected resource footprint in .blackbox.md"
            assert "sk-live-demo-999" not in autopsy, "Secret query param leaked into autopsy"

            reproducer_code = CrashReproducer(wal_ring=ring).generate()
            assert "_blackbox_mock_egress" in reproducer_code, "Expected offline egress stub in reproducer"
            assert "sk-live-demo-999" not in reproducer_code, "Secret leaked into reproducer"

            perf_report = FlakeAnalyzer().analyze_flakiness(wal_path=wal_path)
            assert len(perf_report["n_plus_one_queries"]) >= 1, "Expected N+1 finding"

            heal_plan = AutonomousHealPlanner().build_heal_plan(wal_path=wal_path, root_dir=REPO_ROOT)
            assert len(heal_plan["steps"]) == 4, "Expected 4-step autonomous heal plan"
            assert len(heal_plan["egress_stubs"]) == 1, "Expected 1 sanitized egress stub"
            assert "[REDACTED]" in heal_plan["egress_stubs"][0]["url"]

            print("[Example 04] PASS: Async TraceContext + N+1 SQL + EgressMocker + ResourceProfiler + 4-Step Heal Plan verified.")
            return 0
        finally:
            ring.close()
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
