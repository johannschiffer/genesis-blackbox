# Genesis Blackbox (Flight Recorder & Causal Crash Compiler)

> **Deterministic Runtime Flight Recorder & AST Blast-Radius Compiler for AI-Assisted Software Engineering**  
> *100% Free & Open Source (MIT) &bull; Project Genesis Flagship Architecture*

---

## 1. Executive Summary & Problem Space

Modern AI coding agents (Claude 3.7/Sonnet, Cursor, OpenAI Codex, Windsurf, Antigravity) are extraordinarily capable at static code refactoring, but severely handicapped when debugging complex runtime failures.

### The Diagnostic Chasm
When an autonomous agent attempts to fix a production failure:
1. **Shallow Stack Traces:** Standard tracebacks show line numbers and function calls, but completely obscure runtime local variable states, bind parameters, HTTP request payloads, and network receipts.
2. **Missing Causal Chains:** A crash in a database layer is often caused by an unvalidated payload 5 function calls earlier in an asynchronous pipeline. Standard logs do not correlate cross-module causation.
3. **AST Blind Spots:** Agents frequently modify functions without realizing downstream dependencies, breaking secondary consumers and causing regression cycles.
4. **Token Waste & Hallucination:** Agents waste 50,000+ tokens re-running test suites, adding exploratory print statements, and generating incorrect hypotheses.

---

## 2. The Genesis Blackbox Solution

**Genesis Blackbox** acts as an unyielding, high-performance flight recorder and deterministic autopsy compiler:

```
┌────────────────────────────────────────────────────────────────────────┐
│                        RUNTIME APPLICATION                             │
│   (HTTP Requests / Responses, SQL Queries, Subprocesses, Local State) │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ Sub-millisecond Hooks
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│               1. CIRCULAR WAL RING-BUFFER (wal_ring.py)                │
│   - SQLite WAL Mode (synchronous=NORMAL, temp_store=MEMORY)            │
│   - Circular sliding window (prunes oldest events at capacity limit)   │
│   - Sub-millisecond telemetry ingestion (< 0.2ms write overhead)       │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ Crash Trigger / ExceptionHook
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│            2. DEEP EXCEPTION SERIALIZER (exception_hook.py)            │
│   - Deep frame inspection (frame.f_locals, closure states)             │
│   - Circular reference safety & depth bounding                         │
│   - Automatic credential / PII redaction                               │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│            3. AST BLAST-RADIUS ANALYZER (ast_engine/)                  │
│   - AST graph parser & git diff correlation                            │
│   - Upstream caller & downstream callee impact graph                   │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│          4. CAUSAL CRASH COMPILER (compiler/autopsy.py)                │
│   - Synthesizes WAL execution timeline + deep state + blast radius     │
│   - Emits deterministic, compact `.blackbox.md` Autopsy Capsule        │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                  AI CODING AGENT / MCP INTEGRATION                     │
│   - Agent reads `.blackbox.md` and fixes multi-layer bug on ATTEMPT 1  │
│   - Native MCP Server exposes `blackbox_autopsy` & `blackbox_timeline` │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Four-Phase Engineering Roadmap

### Phase 1: Core Flight Recorder & State Serializer (Current Milestone)
- **`blackbox/recorder/wal_ring.py`**:
  - High-concurrency circular SQLite buffer using Write-Ahead Logging (`WAL`).
  - Thread-safe, non-blocking writes with amortized sub-millisecond latency.
  - Automatic sliding-window circular pruning preserving recent timeline events.
- **`blackbox/recorder/exception_hook.py`**:
  - Deep stack inspection capturing local variable state at crash time.
  - Circular-reference detector (`seen_ids`) preventing infinite recursion.
  - Truncation caps (max depth, max container length, max string length).
  - PII and secret sanitization for API keys, tokens, and authorization headers.
- **Unit Test Suite**: 100% test coverage with sub-second execution.

### Phase 2: AST Blast-Radius Engine & Git Diff Correlation
- **`blackbox/ast_engine/graph.py`**:
  - Python AST and tree-sitter based caller-callee call-graph compiler.
  - Detects modified function signatures and unhandled return types.
- **`blackbox/ast_engine/diff_analyzer.py`**:
  - Computes blast radius of recent `git diff` against codebase call graphs.
  - Flags untested execution paths introduced in the latest commits.

### Phase 3: The Causal Crash Compiler (`.blackbox.md` Capsule)
- **`blackbox/compiler/autopsy.py`**:
  - Correlates runtime exception frames with the WAL ring buffer's last 50 events.
  - Matches SQL queries, HTTP interactions, and state mutations leading up to failure.
- **`.blackbox.md` Specification**:
  - Token-optimized capsule format engineered specifically for LLM attention heads.
  - Zero fluff: precise failure root-cause, variable snapshot diff, affected AST nodes, and suggested fix template.

### Phase 4: Local Time-Travel UI & Model Context Protocol (MCP) Interface
- **`blackbox/mcp_server.py`**:
  - Exposes diagnostic and repair tools to AI coding agents.
- **`blackbox/ui/`**:
  - Zero-dependency local web dashboard for scrubbing execution timelines.

### Phase 5: v0.2.0 Expansion & Polyglot Capabilities (Completed)
- **Polyglot AST Engine & Node/TS Interceptor (`ast_engine/polyglot.py`, `recorder/node_preload.py`)**:
  - Pure-Python structural parser for `.ts`, `.tsx`, `.js`, and `.jsx` extracting imports, exports, functions, and Next.js route boundaries.
  - Active Node.js runtime preload hook wrapping `fetch`, `http.request`, `uncaughtException`, and `unhandledRejection` with secret sanitization.
- **Time-Travel Execution Differ (`compiler/state_differ.py`)**:
  - Differential engine comparing passing baseline executions against failing runs to pinpoint SQL, HTTP, and variable divergence.
- **Polyglot Crash Reproducer & Causal Verifier (`compiler/reproducer.py`, `compiler/verifier.py`)**:
  - Generates standalone `pytest` reproduction tests for Python and `node:test` tests (`.mjs`) for Node/TS crashes.
- **Polyglot Patch Validator (`compiler/patch_validator.py`)**:
  - Evaluates candidate unified diffs with `node --check` and `PolyglotParser`, running reproduction tests with 100% rollback guarantee.
- **AI Repair Prompt Synthesizer (`compiler/prompt_synth.py`)**:
  - Compiles full prompt packets with code fences, autopsy, reproducer, blast radius, and differ via CLI `blackbox pack` and MCP `get_repair_prompt`.
- **Portable Crash Bundler (`recorder/bundle.py`)**:
  - Snapshots WAL via SQLite Online Backup API and generates self-contained `.zip` archives with SHA-256 integrity verification (`blackbox bundle create/inspect`).
- **Live SSE Streaming (`ui/server.py`)**:
  - High-performance `GET /api/live` Server-Sent Events endpoint streaming real-time WAL events to the embedded visualizer.

---

## 4. Architectural Guarantees & Constraints
1. **Zero Runtime Crashing:** The flight recorder must never cause a host application to crash, even under out-of-memory or corrupt state conditions.
2. **Sub-Millisecond Overhead:** Core recording operations must execute in under 1 millisecond.
3. **Deterministic Output:** Autopsy capsules for identical states must produce identical token representations.
4. **100% Free & Open Source:** Licensed under MIT with community sponsorship via Ko-fi.
