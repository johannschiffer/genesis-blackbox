# Genesis Blackbox

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Support on Ko-fi](https://img.shields.io/badge/Support%20on-Ko--fi-ff5e5b.svg)](https://ko-fi.com/johannschiffer)

> **Deterministic Runtime Flight Recorder & AST Blast-Radius Causal Crash Compiler for AI Coding Agents**  
> 100% Free & Open Source under the MIT License. Support independent development on [Ko-fi](https://ko-fi.com/johannschiffer).

---

## What is Genesis Blackbox?

When AI coding agents (Claude, Cursor, Codex) attempt to fix complex runtime exceptions, they waste thousands of tokens hallucinating fixes because they only have shallow stack traces.

**Genesis Blackbox** provides:
1. **Circular SQLite WAL Ring-Buffer (`wal_ring.py`):** Captures high-frequency runtime events (HTTP calls, SQL queries with bind params, subprocesses) with sub-millisecond write latency.
2. **Deep Exception Serializer (`exception_hook.py`):** Captures local variable state across stack frames at crash time with circular-reference safety and secret redaction.
3. **AST Blast-Radius Engine (`ast_engine/`):** Analyzes upstream callers and downstream callee impact.
4. **Causal Crash Compiler (`compiler/`):** Synthesizes timeline events + deep frame states into a deterministic `.blackbox.md` Autopsy Capsule.
5. **Native MCP Server (`mcp_server.py`):** Direct Model Context Protocol interface for AI agents.

---

## Quickstart

```bash
pip install -e .
```

### CLI
```bash
# Record an event
blackbox record --type HTTP_REQ --payload '{"path": "/api/users", "status": 500}'

# View recent timeline
blackbox timeline --limit 20

# Calculate blast radius
blackbox blast-radius --function handle_request --file app.py
```

### MCP Server for Claude Desktop & Cursor
Add to your `claude_desktop_config.json` or `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "blackbox": {
      "command": "python",
      "args": ["-m", "blackbox.mcp_server"]
    }
  }
}
```

---

## Roadmap
See [ROADMAP_BLACKBOX.md](ROADMAP_BLACKBOX.md) for full 4-phase architectural plan.

## License
MIT License. Created by Johann Schiffer.
