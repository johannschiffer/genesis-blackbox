# Example 05: Unified Genesis MCP Stack Operational Incident & Ecosystem Diagnostics

Demonstrates how `genesis-blackbox` v1.0.0 correlates cross-server `MCP_TOOL` telemetry from multiple Genesis MCP servers (`pgsafe-mcp`, `sysflow-mcp`, `crawlengine-mcp`, `browserbridge-mcp`) sharing a single `BLACKBOX_DB_PATH` SQLite WAL:

1. Appends `MCP_TOOL` telemetry events including normal tool calls, a Security Guard block, and an operational error.
2. Runs `GenesisStackManager.diagnose_ecosystem()` to compute per-server counts, error rates, latency stats, and security blocks.
3. Compiles `# ⚠️ BLACKBOX OPERATIONAL INCIDENT CAPSULE` (non-exception failure mode) via `CausalCrashCompiler`.
4. Synthesizes an actionable `AutonomousHealPlanner` remediation plan.

## Run

```bash
python examples/05_genesis_mcp_stack_incident/run_demo.py
```
