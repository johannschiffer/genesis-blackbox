# Example 04: Async TraceContext + N+1 SQL + Offline Egress Mock + 4-Step Heal Plan

Demonstrates the full `genesis-blackbox` v1.0.0 diagnostic pipeline inside an isolated temporary sandbox (100% offline, using a local ephemeral `HTTPServer` on `127.0.0.1:0`):

1. Executes an async order enrichment workflow under `TraceContext`, `EgressMocker`, `ResourceProfiler`, and `SqlInterceptor`.
2. Triggers an N+1 SQL query loop (`>= 6` repeated `SELECT` queries) and an outbound HTTP call containing `?api_key=...` (automatically redacted in the WAL).
3. Captures an unhandled `TypeError` via `ExceptionHook`.
4. Compiles `.blackbox.md` (with N+1 + Resource Footprint sections), synthesizes an offline `test_blackbox_reproduce.py` embedding `unittest.mock` HTTP egress stubs, and builds the deterministic 4-phase `AutonomousHealPlanner` plan.

## Run

```bash
python examples/04_async_n1_egress_heal_loop/run_demo.py
```
