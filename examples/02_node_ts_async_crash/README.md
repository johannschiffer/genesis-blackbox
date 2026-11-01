# Showcase 2: Node.js / TypeScript Runtime Preload & V8 Reproducer

Demonstrates `genesis-blackbox` v0.2.0 polyglot flight recorder features on a Node.js/TypeScript asynchronous microservice.

---

## The Scenario

1. **Active Preload Hook (`NODE_OPTIONS`):**
   - Automatically injected when running `blackbox record -- node app.mjs`.
   - Zero external dependencies: hooks `globalThis.fetch`, `http.request`, `uncaughtException`, and `unhandledRejection`.

2. **Automated Credential Redaction:**
   - Outbound `fetch` requests containing sensitive tokens (`Authorization: Bearer ...`, `Cookie: ...`, `?api_key=...`) are sanitized in-flight before being committed to WAL.
   - Non-sensitive headers (`X-Service-Name`, `X-Custom-Trace`) are preserved intact.

3. **Polyglot AST Blast-Radius Engine:**
   - Pure-Python parser reads ESM and TypeScript files (`app.mjs`, `routes.ts`) without requiring Node or Babel.
   - Discovers call dependencies: `handleCheckout()` in `app.mjs` calls `processOrderRoute()` in `routes.ts`.

4. **V8 Crash Ingestion & Autonomous Reproducer:**
   - When an unhandled rejection occurs in `routes.ts` (`TypeError: Cannot read properties of undefined (reading 'tier')`), the V8 stack trace is parsed into structured frames.
   - `CrashReproducer` automatically generates a native, standalone Node.js reproduction script (`test_blackbox_reproduce.mjs`) using `node:test`.

---

## 1-Line Execution

```bash
python examples/02_node_ts_async_crash/run_demo.py
```

Or using the CLI directly:

```bash
# 1. Run target with flight recorder
blackbox record --db .blackbox/wal.sqlite -- node examples/02_node_ts_async_crash/app.mjs

# 2. Calculate polyglot blast radius
blackbox blast-radius examples/02_node_ts_async_crash/routes.ts --symbol processOrderRoute

# 3. Synthesize reproduction script
blackbox reproduce --db .blackbox/wal.sqlite --output test_blackbox_reproduce.mjs

# 4. Run synthesized reproduction test
node --test test_blackbox_reproduce.mjs
```

---

## Key Generated Artifacts

- `.blackbox.md`: Autopsy capsule with V8 stack trace, TypeScript code snippet, and sanitized HTTP events.
- `test_blackbox_reproduce.mjs`: Standalone `node:test` reproduction suite.

