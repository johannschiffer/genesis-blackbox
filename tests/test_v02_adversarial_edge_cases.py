"""
Genesis Blackbox: Adversarial Stress Test & Edge-Case Hardening Test Suite (v0.2.0)
Tests hostile inputs, path traversals, AST tricks, non-Error throws, NaN transitions,
and archive corruption across polyglot, differ, runner, reproducer, validator, and bundler.
"""

import os
import sys
import json
import math
import shutil
import zipfile
import hashlib
import tempfile
import subprocess
import pytest

from blackbox.recorder.wal_ring import WalRingBuffer
from blackbox.ast_engine.polyglot import PolyglotParser
from blackbox.ast_engine.graph import CodebaseGraph
from blackbox.ast_engine.impact import ImpactTestSelector
from blackbox.compiler.state_differ import StateDiffer
from blackbox.compiler.patch_validator import PatchValidator
from blackbox.compiler.reproducer import CrashReproducer
from blackbox.recorder.bundle import CrashBundler, inspect_bundle
from blackbox.recorder.node_preload import write_node_preload_script, ingest_node_spool, NODE_PRELOAD_CODE
from blackbox.recorder.runner import run_target


def test_polyglot_adversarial_fake_imports_exports_in_comments_and_strings():
    """
    Ensures comments (single-line & multiline) and string literals containing fake
    import/export statements are not mistakenly extracted as AST dependencies.
    """
    adversarial_code = """
    // import { fakeA } from "malicious-package-a";
    // require("hidden-crypto-miner");
    /*
     * import { fakeB } from "malicious-package-b";
     * export const hacked = () => true;
     * export default backdoor;
     */
    const fakeImportString = "import { fakeC } from 'fake-module-c'";
    const fakeRequireString = 'const x = require("fake-module-d")';
    const fakeExportString = "export function fakeExport() {}";

    // Legitimate code
    import { realService } from "./realService";
    const logger = require("./logger");

    export function legitimateFunction(x: number): number {
        const query = "SELECT * FROM users WHERE note = '// import fake from nowhere'";
        return x * 2;
    }

    export const legitimateExport = 42;
    """
    parser = PolyglotParser(file_path="service.ts")
    res = parser.parse(adversarial_code)

    # Verify imports
    imported_mods = res["imported_modules"]
    assert "./realService" in imported_mods
    assert "./logger" in imported_mods

    # Adversarial strings/comments must NOT appear
    assert "malicious-package-a" not in imported_mods
    assert "malicious-package-b" not in imported_mods
    assert "hidden-crypto-miner" not in imported_mods
    assert "fake-module-c" not in imported_mods
    assert "fake-module-d" not in imported_mods

    # Verify exported symbols
    export_names = [e["name"] for e in res["exports"]]
    assert "legitimateFunction" in export_names
    assert "legitimateExport" in export_names
    assert "hacked" not in export_names
    assert "backdoor" not in export_names
    assert "fakeExport" not in export_names


def test_polyglot_template_literals_with_interpolations_and_validation():
    """
    Tests template literals with nested expressions, backticks, and ${...} interpolations.
    Verifies that syntax validation parses balanced structures without false positives.
    """
    valid_template_code = """
    const prefix = "ITEM_";
    const count = 10;
    const formatItem = (id: string) => {
        const label = `Label: ${prefix}${id}_${count * 2}`;
        const query = `SELECT * FROM items WHERE id = ${getId({ id: `nested_${id}` })} AND active = 1`;
        return `${label}: [${query}]`;
    };
    export default formatItem;
    """
    parser = PolyglotParser(file_path="template.ts")
    # Must not raise SyntaxError
    parser.validate_syntax(valid_template_code)
    res = parser.parse(valid_template_code)
    assert "formatItem" in res["functions"]

    # Adversarial unclosed template literal
    unclosed_template = """
    const bad = `Unclosed template string starts here
    and keeps going without ending backtick;
    function shouldFail() { return 1; }
    """
    with pytest.raises(SyntaxError, match="Unclosed template literal"):
        parser.validate_syntax(unclosed_template)

    # Adversarial unbalanced brace inside interpolation
    unbalanced_interpolation = """
    const badInterpolation = `Result: ${ { a: 1 `;
    """
    with pytest.raises(SyntaxError):
        parser.validate_syntax(unbalanced_interpolation)


def test_polyglot_typescript_generics_and_multiline_arrow_functions():
    """
    Tests parsing of TypeScript generics with complex constraints and multiline arrow functions.
    """
    ts_code = """
    import { BaseEntity } from './models';

    export async function fetchEntity<T extends BaseEntity<K>, K = string>(
        entityId: K,
        options?: RequestOptions<T>
    ): Promise<T | null> {
        return queryDatabase(entityId);
    }

    export const processTransform = <T, R = T>(
        item: T,
        mapper: (val: T) => R
    ): R => {
        return mapper(item);
    };

    export const multilineArrow = (x: number, y: number) =>
        computePrimary(x)
        + computeSecondary(y);
    """
    parser = PolyglotParser(file_path="generics.ts")
    res = parser.parse(ts_code)

    assert "fetchEntity" in res["functions"]
    assert "processTransform" in res["functions"]
    assert "multilineArrow" in res["functions"]

    # Check calls extracted from multiline arrow
    calls = res["functions"]["multilineArrow"]["calls"]
    assert "computePrimary" in calls or "computeSecondary" in calls


def test_polyglot_cyclic_dependency_graph_and_blast_radius():
    """
    Tests that cyclic dependencies between TypeScript files do not cause recursion errors
    during blast radius calculation and test impact selection.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        mod_a = os.path.join(tmp_dir, "moduleA.ts")
        mod_b = os.path.join(tmp_dir, "moduleB.ts")
        test_file = os.path.join(tmp_dir, "moduleA.test.ts")

        # Circular import: A imports B, B imports A
        with open(mod_a, "w") as f:
            f.write("""
            import { funcB } from './moduleB';
            export function funcA(x: number): number {
                return funcB(x) + 1;
            }
            """)

        with open(mod_b, "w") as f:
            f.write("""
            import { funcA } from './moduleA';
            export function funcB(x: number): number {
                if (x <= 0) return 0;
                return funcA(x - 1);
            }
            """)

        with open(test_file, "w") as f:
            f.write("""
            import { funcA } from './moduleA';
            export function testFuncA() {
                funcA(5);
            }
            """)

        graph = CodebaseGraph()
        graph.parse_file(mod_a)
        graph.parse_file(mod_b)

        # Blast radius must terminate cleanly without RecursionError
        radius_a = graph.get_blast_radius("funcA", depth=5, file_path=mod_a)
        assert "funcA" in graph.nodes
        assert "funcB" in graph.nodes
        assert "funcB" in radius_a["downstream_callees"] or "funcB" in radius_a["upstream_callers"]

        # Impact test selector resolves moduleA.test.ts
        selector = ImpactTestSelector(root_dir=tmp_dir, graph=graph)
        impacted = selector.find_impacted_tests(files=[mod_a], diff=False)
        assert any("moduleA.test.ts" in t for t in impacted)


def test_node_preload_env_preservation_and_sensitive_redaction():
    """
    Verifies that runner.py preserves existing user NODE_OPTIONS and that
    node_preload.py sanitizes sensitive headers and URLs.
    """
    # 1. Test runner NODE_OPTIONS preservation
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "wal.sqlite")
        target_script = os.path.join(tmp_dir, "check_env.js")
        with open(target_script, "w") as f:
            f.write("""
            const opts = process.env.NODE_OPTIONS || '';
            if (!opts.includes('--max-old-space-size=2048')) {
                process.exit(2);
            }
            if (!opts.includes('--require')) {
                process.exit(3);
            }
            process.exit(0);
            """)

        env = os.environ.copy()
        env["NODE_OPTIONS"] = "--max-old-space-size=2048"

        # Run with patched env
        old_env = os.environ.get("NODE_OPTIONS")
        try:
            os.environ["NODE_OPTIONS"] = "--max-old-space-size=2048"
            ret = run_target(["node", target_script], db_path=db_path)
            assert ret == 0
        finally:
            if old_env is not None:
                os.environ["NODE_OPTIONS"] = old_env
            else:
                os.environ.pop("NODE_OPTIONS", None)

    # 2. Test sensitive key redaction in Node.js preload runtime
    test_node_sanitization_script = f"""
    {NODE_PRELOAD_CODE}

    // Verify isSensitiveKey
    if (!isSensitiveKey('authorization')) process.exit(10);
    if (!isSensitiveKey('X-Api-Key')) process.exit(11);
    if (!isSensitiveKey('client_secret')) process.exit(12);
    if (!isSensitiveKey('jwt_token')) process.exit(13);
    if (isSensitiveKey('content-type')) process.exit(14);

    // Verify sanitizeHeaders
    const headers = sanitizeHeaders({{
        'Content-Type': 'application/json',
        'X-API-Key': 'super-secret-12345',
        'Authorization': 'Bearer abcdef'
    }});
    if (headers['Content-Type'] !== 'application/json') process.exit(20);
    if (!headers['X-API-Key'].includes('REDACTED')) process.exit(21);
    if (!headers['Authorization'].includes('REDACTED')) process.exit(22);

    // Verify sanitizeUrl
    const sanitizedUrl = sanitizeUrl('https://api.example.com/v1/charge?token=tok_live_999&amount=500&client_secret=sec_42');
    if (!sanitizedUrl.includes('token=REDACTED')) process.exit(30);
    if (!sanitizedUrl.includes('client_secret=REDACTED')) process.exit(31);
    if (!sanitizedUrl.includes('amount=500')) process.exit(32);

    process.exit(0);
    """
    res = subprocess.run(["node", "-e", test_node_sanitization_script], capture_output=True, text=True)
    assert res.returncode == 0, f"Node sanitization test failed: {res.stderr}"


def test_node_preload_non_error_throws_and_rejections():
    """
    Tests that non-Error throws in Node (e.g. `throw "string"` or `Promise.reject(null)`)
    are normalized into structured EXCEPTION events without crashing the preload handler.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "wal.sqlite")

        # 1. String throw
        str_throw_script = os.path.join(tmp_dir, "str_throw.js")
        with open(str_throw_script, "w") as f:
            f.write("""
            setTimeout(() => {
                throw "uncaught string exception payload";
            }, 50);
            """)

        ret = run_target(["node", str_throw_script], db_path=db_path)
        assert ret != 0

        ring = WalRingBuffer(db_path=db_path)
        excs = ring.query_recent(limit=5, event_type="EXCEPTION")
        ring.close()

        assert len(excs) >= 1
        exc_payload = excs[0]["payload"]
        assert "uncaught string exception payload" in exc_payload["exception_message"]

        # 2. Null promise rejection
        db_path2 = os.path.join(tmp_dir, "wal2.sqlite")
        null_reject_script = os.path.join(tmp_dir, "null_reject.js")
        with open(null_reject_script, "w") as f:
            f.write("""
            setTimeout(() => {
                Promise.reject(null);
            }, 50);
            """)

        ret2 = run_target(["node", null_reject_script], db_path=db_path2)
        assert ret2 != 0

        ring2 = WalRingBuffer(db_path=db_path2)
        excs2 = ring2.query_recent(limit=5, event_type="EXCEPTION")
        ring2.close()

        assert len(excs2) >= 1
        exc_payload2 = excs2[0]["payload"]
        assert exc_payload2["exception_message"] == "null"


def test_state_differ_sql_whitespace_insensitivity():
    """
    Ensures SQL queries differing solely by whitespace/newlines are normalized
    and not falsely classified as SQL structural divergence.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "wal.sqlite")
        ring = WalRingBuffer(db_path=db_path)

        # Baseline run: nicely formatted multiline SQL
        ring.record("PROCESS_START", {"run_id": "run-pass-1"}, source="test")
        ring.record("SQL_QUERY", {
            "query": "SELECT id, name, email\n  FROM users\n  WHERE status = 'active'\n  ORDER BY id DESC",
            "params": [10]
        }, status="ok", source="test")
        ring.record("PROCESS_EXIT", {"run_id": "run-pass-1", "exit_code": 0}, source="test")

        # Crashing run: single-line formatted SQL with different spacing
        ring.record("PROCESS_START", {"run_id": "run-fail-1"}, source="test")
        ring.record("SQL_QUERY", {
            "query": "SELECT   id,   name, email FROM users WHERE   status = 'active' ORDER BY id DESC",
            "params": [10]
        }, status="ok", source="test")
        ring.record("EXCEPTION", {
            "exception_type": "ValueError",
            "exception_message": "Downstream processing error"
        }, status="error", source="test")
        ring.record("PROCESS_EXIT", {"run_id": "run-fail-1", "exit_code": 1}, source="test")

        differ = StateDiffer(wal_ring=ring)
        diff_res = differ.diff(baseline_run_id="run-pass-1", failing_run_id="run-fail-1")
        differ.close()

        # The SQL query should NOT be considered diverged
        sql_diffs = diff_res["sql_diff"]
        assert len(sql_diffs) == 1
        assert sql_diffs[0]["divergence"] is None

        # The divergence point should be the EXCEPTION, not the SQL query formatting
        div = diff_res["divergence_point"]
        assert div is not None
        assert div["type"] == "UNCAUGHT_EXCEPTION"


def test_state_differ_nan_transitions_and_nested_param_diffing():
    """
    Verifies that StateDiffer correctly tracks mutations involving float('nan')
    and performs deep parameter schema comparisons without infinite recursion.
    """
    differ = StateDiffer.__new__(StateDiffer)

    # 1. NaN vs NaN should match
    assert differ._diff_param_shapes([float("nan")], [float("nan")]) is None

    # 2. Float to NaN mutation
    diff_to_nan = differ._diff_param_shapes([100.5], [float("nan")])
    assert diff_to_nan is not None
    assert "mutated to NaN" in diff_to_nan

    # 3. NaN to Float mutation
    diff_from_nan = differ._diff_param_shapes([float("nan")], [100.5])
    assert diff_from_nan is not None
    assert "mutated from NaN to float" in diff_from_nan

    # 4. Deeply nested param mutation
    nested_b = {"user": {"profile": {"scores": [1, 2, 3]}}}
    nested_f = {"user": {"profile": {"scores": [1, None, 3]}}}
    nested_diff = differ._diff_param_shapes(nested_b, nested_f)
    assert nested_diff is not None
    assert "mutated to None" in nested_diff

    # 5. Named param NaN mutation
    dict_b = {"tax_rate": 0.15}
    dict_f = {"tax_rate": float("nan")}
    dict_diff = differ._diff_param_shapes(dict_b, dict_f)
    assert dict_diff is not None
    assert "mutated to NaN" in dict_diff


def test_state_differ_zero_events_and_malformed_timestamps():
    """
    Tests edge cases where runs contain 0 events or malformed timestamps.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "wal.sqlite")
        ring = WalRingBuffer(db_path=db_path)

        ring.record("PROCESS_START", {"run_id": "empty-pass"}, source="test")
        ring.record("PROCESS_EXIT", {"run_id": "empty-pass", "exit_code": 0}, source="test")

        ring.record("PROCESS_START", {"run_id": "empty-fail"}, source="test")
        ring.record("EXCEPTION", {"exception_type": "RuntimeError", "exception_message": "crash"}, source="test")
        ring.record("PROCESS_EXIT", {"run_id": "empty-fail", "exit_code": 1}, source="test")

        differ = StateDiffer(wal_ring=ring)
        diff_res = differ.diff(baseline_run_id="empty-pass", failing_run_id="empty-fail")

        # Corrupt timestamps in result dictionary
        diff_res["baseline_run"]["timestamp"] = "malformed_timestamp_string"
        diff_res["failing_run"]["timestamp"] = None

        # compile_markdown should handle corrupted timestamps safely
        md = differ.compile_markdown(diff_res)
        differ.close()

        assert "# ⏱️ BLACKBOX TIME-TRAVEL EXECUTION DIFF" in md
        assert "empty-pass" in md
        assert "empty-fail" in md


def test_patch_validator_path_traversal_rejection():
    """
    Verifies that PatchValidator strictly rejects patches attempting directory traversal
    outside of the repository root.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        # Initialize dummy git repo
        subprocess.run(["git", "init"], cwd=tmp_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_dir, check=True)

        target_file = os.path.join(tmp_dir, "main.py")
        with open(target_file, "w") as f:
            f.write("def foo():\n    return 1\n")
        subprocess.run(["git", "add", "."], cwd=tmp_dir, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_dir, check=True)

        validator = PatchValidator(repo_root=tmp_dir)

        # Path traversal attack patch
        traversal_patch = """
diff --git a/../../../../etc/passwd b/../../../../etc/passwd
--- a/../../../../etc/passwd
+++ b/../../../../etc/passwd
@@ -1,1 +1,1 @@
-root:x:0:0:root:/root:/bin/bash
+hacked:x:0:0:root:/root:/bin/bash
"""
        # 1. Dry run must reject
        assert validator.validate_patch_dry_run(traversal_patch) is False

        # 2. Evaluation must abort with security error
        eval_res = validator.evaluate_patch(traversal_patch)
        assert eval_res["success"] is False
        assert "Security violation" in eval_res["output"]


def test_patch_validator_missing_eof_newline_handling():
    """
    Verifies that candidate patches missing a trailing newline are automatically
    normalized and applied cleanly.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        subprocess.run(["git", "init"], cwd=tmp_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_dir, check=True)

        target_file = os.path.join(tmp_dir, "calc.py")
        with open(target_file, "w") as f:
            f.write("def add(a, b):\n    return a + b\n")
        subprocess.run(["git", "add", "."], cwd=tmp_dir, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_dir, check=True)

        # Patch without trailing newline
        raw_patch = (
            "diff --git a/calc.py b/calc.py\n"
            "--- a/calc.py\n"
            "+++ b/calc.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def add(a, b):\n"
            "-    return a + b\n"
            "+    return (a + b) * 1"
        )
        assert not raw_patch.endswith("\n")

        validator = PatchValidator(repo_root=tmp_dir)
        # Should apply cleanly despite missing trailing newline
        assert validator.validate_patch_dry_run(raw_patch) is True


def test_crash_bundle_corrupted_archive_and_tampered_members():
    """
    Verifies that inspect_bundle cleanly handles non-zip files, missing manifests,
    tampered/corrupted members, and non-dictionary checksum metadata.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        bundler = CrashBundler(repo_root=tmp_dir)

        # 1. Non-zip corrupted file
        fake_zip = os.path.join(tmp_dir, "fake.zip")
        with open(fake_zip, "wb") as f:
            f.write(b"NOT_A_ZIP_HEADER_DATA_12345")

        res_fake = inspect_bundle(fake_zip)
        assert res_fake["valid"] is False
        assert "not a valid zip" in res_fake["error"]

        # 2. Missing system_manifest.json
        no_manifest_zip = os.path.join(tmp_dir, "no_manifest.zip")
        with zipfile.ZipFile(no_manifest_zip, "w") as zf:
            zf.writestr("test.txt", "hello")

        res_no_manifest = inspect_bundle(no_manifest_zip)
        assert res_no_manifest["valid"] is False
        assert "missing system_manifest.json" in res_no_manifest["error"]

        # 3. Tampered archive member (SHA mismatch)
        tampered_zip = os.path.join(tmp_dir, "tampered.zip")
        manifest = {
            "checksums": {
                "fileA.txt": hashlib.sha256(b"original content").hexdigest()
            }
        }
        with zipfile.ZipFile(tampered_zip, "w") as zf:
            zf.writestr("system_manifest.json", json.dumps(manifest))
            zf.writestr("fileA.txt", b"tampered evil content")

        res_tampered = inspect_bundle(tampered_zip)
        assert res_tampered["valid"] is False
        assert len(res_tampered["corrupted_files"]) == 1
        assert "mismatch" in res_tampered["corrupted_files"][0]["error"]

        # 4. Malformed checksums (non-dict)
        bad_checksums_zip = os.path.join(tmp_dir, "bad_checksums.zip")
        bad_manifest = {
            "checksums": ["invalid", "list", "instead", "of", "dict"]
        }
        with zipfile.ZipFile(bad_checksums_zip, "w") as zf:
            zf.writestr("system_manifest.json", json.dumps(bad_manifest))

        res_bad_checksums = inspect_bundle(bad_checksums_zip)
        assert res_bad_checksums["valid"] is False
        assert "checksums" in res_bad_checksums["error"]


def test_reproducer_anonymous_node_target_fallback():
    """
    Verifies that CrashReproducer handles Node crashes where root_cause function
    is '<anonymous>', '<module>', or empty by searching frame history and generating
    a valid loadTargetFunction fallback.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "wal.sqlite")
        target_js = os.path.join(tmp_dir, "handler.mjs")
        with open(target_js, "w") as f:
            f.write("""
            export function processOrder(order) {
                if (!order.id) throw new TypeError("Missing order id");
                return order.id;
            }
            """)

        ring = WalRingBuffer(db_path=db_path)
        ring.record("EXCEPTION", {
            "runtime": "node",
            "exception_type": "TypeError",
            "exception_message": "Missing order id",
            "root_cause": {
                "filename": target_js,
                "function": "<anonymous>",
                "lineno": 3,
                "locals": {"order": {}}
            },
            "frames": [
                {
                    "filename": target_js,
                    "function": "processOrder",
                    "lineno": 2
                },
                {
                    "filename": target_js,
                    "function": "<anonymous>",
                    "lineno": 3
                }
            ]
        }, source="test")

        reproducer = CrashReproducer(wal_ring=ring)
        code = reproducer.generate()
        ring.close()

        # Should resolve processOrder from frames rather than leaving <anonymous>
        assert "processOrder" in code
        assert "loadTargetFunction" in code
