# Showcase 1: Python Time-Travel SQL & State Divergence

Demonstrates `genesis-blackbox` v0.2.0 time-travel execution diffing across two consecutive runs of a Python + SQLite order processing pipeline.

---

## The Scenario

1. **Run 1 (Healthy Baseline):**
   - Outbound HTTP payment gateway authorization check (`POST https://api.payments.internal/v1/checkout/auth`) with sensitive Bearer token redaction.
   - Database schema creation: `orders (id, user_id, item, total, discount CHECK(discount >= 0.0 AND discount <= 1.0))`.
   - Successful insertion of order batch with valid discount multiplier (`0.10` and `0.05`).
   - Recorded cleanly into WAL ring-buffer as a passing execution run.

2. **Run 2 (Failing Run):**
   - Upstream API parameter shift: the discount multiplier mutates to `None`.
   - SQLite execution fails with `sqlite3.IntegrityError: NOT NULL constraint failed: orders.discount`.
   - Deep exception serializer captures local variables at point of failure (`user_id=43`, `discount=None`, `total=50.0`).
   - Recorded in WAL ring-buffer with status `error`.

3. **Causal Crash Compilation & State Differ:**
   - `CausalCrashCompiler` produces `.blackbox.md` (autopsy capsule with AST context and error trace).
   - `StateDiffer` aligns chronological event streams from Run 1 and Run 2, pinpointing the exact step where execution diverged (`SQL_QUERY_FAILURE`), the mutated bound parameter (`discount: Value mutated to None`), and outputs `.blackbox-diff.md`.

---

## 1-Line Execution

```bash
python examples/01_python_time_travel_diff/run_demo.py
```

Or using the CLI directly:

```bash
# 1. Run baseline
blackbox record --db .blackbox/wal.sqlite -- python examples/01_python_time_travel_diff/pipeline.py --mode baseline

# 2. Run failing
blackbox record --db .blackbox/wal.sqlite -- python examples/01_python_time_travel_diff/pipeline.py --mode failing

# 3. Compare runs
blackbox diff-runs --db .blackbox/wal.sqlite --output .blackbox-diff.md

# 4. Generate crash autopsy
blackbox autopsy --db .blackbox/wal.sqlite --output .blackbox.md
```

---

## Key Generated Artifacts

- `.blackbox.md`: Autopsy capsule with stack frames, sanitized HTTP headers, and failing SQL query.
- `.blackbox-diff.md`: Markdown table and divergence analysis pinpointing Step #4 and variable mutations.

