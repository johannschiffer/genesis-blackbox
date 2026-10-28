# Genesis Blackbox

[![PyPI version](https://img.shields.io/pypi/v/genesis-blackbox.svg)](https://pypi.org/project/genesis-blackbox/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Support on Ko-fi](https://img.shields.io/badge/Support%20on-Ko--fi-ff5e5b.svg)](https://ko-fi.com/johannschiffer)

Deterministic runtime flight recorder and AST blast-radius causal crash compiler for Python services and coding agents.

---

## Overview

When coding agents (Claude Code, Cursor, Windsurf) encounter runtime failures, plain stack traces omit the HTTP/SQL/subprocess events that preceded the error, the local variable bindings inside each frame, and the static call-graph impact of a proposed fix.

`genesis-blackbox` records runtime context with zero external runtime dependencies:

1. **SQLite WAL Ring-Buffer (`wal_ring.py`):** Bounded circular log for HTTP transactions, SQL queries with redacted bind parameters, subprocess invocations, and exceptions.
2. **Deep Exception Serializer (`exception_hook.py`):** Captures per-frame local variable scopes across `sys.excepthook` and `threading.excepthook` with circular-reference bounds and secret redaction.
3. **Zero-Config & Framework Instrumentation (`instrumentation.py`, `integrations/`):** Drop-in `auto_instrument()` plus idiomatic adapters for FastAPI/Starlette (ASGI), Flask (WSGI), sqlite3/DB-API, and SQLAlchemy.
4. **AST Blast-Radius & Impact Selector (`ast_engine/`):** Static call-graph analyzer that computes upstream callers, downstream callees, and impacted pytest files for modified code.
5. **Causal Crash Compiler, Reproducer & Patch Validator (`compiler/`):** Compiles `.blackbox.md` autopsy capsules, synthesizes standalone pytest reproduction scripts, validates candidate `git diff` patches with guaranteed rollback, and writes `.blackbox-resolved.md` verification receipts.
6. **Pytest Plugin & GitHub Actions Summary (`pytest_plugin.py`, `ci/`):** Native `pytest --blackbox` hook and collapsible `$GITHUB_STEP_SUMMARY` Markdown exporter.
7. **Native MCP Server & Local Web Inspector (`mcp_server.py`, `ui/`):** Stdio MCP server (`blackbox-mcp`) exposing 7 diagnostic tools alongside a zero-dependency localhost timeline viewer.

---

## Architecture

```mermaid
flowchart LR
    subgraph Runtime ["Runtime Interceptors"]
        HTTP["HTTP (ASGI / WSGI)"]
        SQL["SQL (DB-API / SQLAlchemy)"]
        PROC["Subprocess & Exceptions"]
    end

    WAL[("SQLite WAL Ring-Buffer")]
    AST["AST Blast-Radius & Impact Engine"]
    Compiler["Causal Crash Compiler"]
    Capsule[(".blackbox.md + Reproducer Test")]
    Validator["Patch Validator & Receipt Verifier"]

    HTTP --> WAL
    SQL --> WAL
    PROC --> WAL
    WAL --> Compiler
    AST --> Compiler
    Compiler --> Capsule
    Capsule --> Validator
```

---

## Quickstart

### 1. Installation

```bash
pip install genesis-blackbox
```

### 2. Record Any Python Command or Service

```bash
blackbox record -- python app.py
```

Or attach inside application code with one line:

```python
from blackbox import auto_instrument

auto_instrument(db_path=".blackbox/wal.sqlite")
```

Framework-specific helpers are also included:

```python
from blackbox import instrument_fastapi, instrument_flask, instrument_engine

instrument_fastapi(fastapi_app)
instrument_flask(flask_app)
instrument_engine(sqlalchemy_engine)
```

### 3. Pytest Plugin & Impact Test Selection

Capture autopsies and standalone reproduction scripts automatically on test failures:

```bash
pytest --blackbox
```

Select and execute only the tests affected by modified source symbols:

```bash
blackbox test-impact --diff --run
```

### 4. Compile Autopsies, Reproduce, and Verify Fixes

```bash
# Compile .blackbox.md from the latest crash in .blackbox/wal.sqlite
blackbox autopsy

# Synthesize a standalone pytest reproduction script
blackbox reproduce --output test_blackbox_reproduce.py

# Run the reproduction test and emit .blackbox-resolved.md once fixed
blackbox verify --test test_blackbox_reproduce.py

# Export collapsible Markdown to GitHub Actions $GITHUB_STEP_SUMMARY
blackbox ci-summary --autopsy .blackbox.md

# Initialize agent workflow rules (.cursorrules, CLAUDE.md, or .windsurfrules)
blackbox init-rules --type claude

# Launch the localhost timeline and autopsy web inspector
blackbox ui --port 8765 --no-browser
```

---

## MCP Server Configuration

`genesis-blackbox` ships the `blackbox-mcp` stdio server exposing 7 MCP tools:

- `get_crash_autopsy`: Compiles `.blackbox.md` from recorded WAL events.
- `generate_reproduction_test`: Synthesizes an executable pytest reproduction script from the latest recorded exception.
- `verify_crash_resolution`: Executes the reproduction test and emits `.blackbox-resolved.md` when resolved.
- `test_candidate_patch`: Evaluates a unified `git diff` against reproduction and AST-impacted tests with automatic rollback.
- `get_blast_radius`: Computes upstream callers and downstream callees for a Python file or symbol.
- `query_execution_timeline`: Queries recent HTTP, SQL, subprocess, and exception events.
- `record_event`: Appends structured execution telemetry to the circular WAL buffer.

### Claude Desktop / Cursor / Windsurf

```json
{
  "mcpServers": {
    "blackbox": {
      "command": "uvx",
      "args": ["--from", "genesis-blackbox", "blackbox-mcp"]
    }
  }
}
```

If installed into an active virtual environment, you can invoke `blackbox-mcp` directly:

```json
{
  "mcpServers": {
    "blackbox": {
      "command": "blackbox-mcp",
      "args": ["--db", ".blackbox/wal.sqlite"]
    }
  }
}
```

---

## Support

If this project is useful in your engineering workflow, you can support ongoing open-source maintenance on [Ko-fi](https://ko-fi.com/johannschiffer).

---

## License

Licensed under the [MIT License](LICENSE). Copyright (c) Johann Schiffer.
