"""
Genesis Blackbox: v0.3.0 Consolidation & Regression Tests
Covers:
1. CrashReproducer `_invoke` signature filtering (sync with extra locals, **kwargs, async coroutines).
2. StateDiffer `http_transaction` payload/path mutation detection.
3. WalRingBuffer `close()` final pruning when exceeding `max_records` below `prune_batch_size`.
4. Version consistency across package, MCP server, and metadata.
"""

import subprocess
import sys
from pathlib import Path

import blackbox
from blackbox.compiler.reproducer import CrashReproducer
from blackbox.compiler.state_differ import StateDiffer
from blackbox.mcp_server import BlackboxMCPServer
from blackbox.recorder.wal_ring import WalRingBuffer


def _record_crash(ring: WalRingBuffer, filename: str, func_name: str, locals_dict: dict, exc_type: str = "ValueError", exc_msg: str = "boom"):
    ring.record(
        event_type="EXCEPTION",
        payload={
            "exception_type": exc_type,
            "exception_message": exc_msg,
            "traceback": f"Traceback...\n{exc_type}: {exc_msg}\n",
            "root_cause": {
                "filename": filename,
                "function": func_name,
                "lineno": 5,
                "locals": locals_dict,
            },
        },
        source="exception_hook",
        status="error",
    )


def test_reproducer_invoke_filters_extra_locals_and_handles_kwargs(tmp_path: Path):
    target = tmp_path / "service.py"
    target.write_text(
        "def compute_discount(price, rate):\n"
        "    if rate < 0:\n"
        "        raise ValueError('negative rate')\n"
        "    return price * (1 - rate)\n",
        encoding="utf-8",
    )

    ring = WalRingBuffer(db_path=str(tmp_path / "wal.sqlite"))
    try:
        # Include an extra local variable (`internal_cache`) that is NOT in function signature.
        _record_crash(
            ring,
            str(target),
            "compute_discount",
            {"price": 100, "rate": -0.2, "internal_cache": {"unused": True}},
        )
        repro = CrashReproducer(ring)
        test_code = repro.generate()
        assert "def _invoke(func, kwargs):" in test_code
        assert "del result" in test_code

        test_file = tmp_path / "test_repro_sync.py"
        test_file.write_text(test_code, encoding="utf-8")

        # Unfixed code should fail with ValueError (not TypeError from extra local)
        res_fail = subprocess.run(
            [sys.executable, "-m", "pytest", str(test_file), "-q"],
            capture_output=True,
            text=True,
        )
        assert res_fail.returncode == 1
        assert "ValueError: negative rate" in res_fail.stdout + res_fail.stderr

        # Fixed code with valid input / fix passes cleanly without TypeError on extra locals
        target.write_text(
            "def compute_discount(price, rate):\n"
            "    rate = max(0.0, rate)\n"
            "    return price * (1 - rate)\n",
            encoding="utf-8",
        )
        res_pass = subprocess.run(
            [sys.executable, "-m", "pytest", str(test_file), "-q"],
            capture_output=True,
            text=True,
        )
        assert res_pass.returncode == 0, res_pass.stdout + res_pass.stderr
    finally:
        ring.close()


def test_reproducer_invoke_awaits_async_coroutine(tmp_path: Path):
    target = tmp_path / "async_service.py"
    target.write_text(
        "async def process_order(order_id):\n"
        "    if order_id <= 0:\n"
        "        raise RuntimeError('invalid order_id')\n"
        "    return {'order_id': order_id, 'ok': True}\n",
        encoding="utf-8",
    )

    ring = WalRingBuffer(db_path=str(tmp_path / "wal_async.sqlite"))
    try:
        _record_crash(
            ring,
            str(target),
            "process_order",
            {"order_id": 0, "ephemeral": "ignored"},
            exc_type="RuntimeError",
            exc_msg="invalid order_id",
        )
        repro = CrashReproducer(ring)
        test_file = tmp_path / "test_repro_async.py"
        test_file.write_text(repro.generate(), encoding="utf-8")

        res_fail = subprocess.run(
            [sys.executable, "-m", "pytest", str(test_file), "-q"],
            capture_output=True,
            text=True,
        )
        assert res_fail.returncode == 1
        assert "RuntimeError: invalid order_id" in res_fail.stdout + res_fail.stderr

        target.write_text(
            "async def process_order(order_id):\n"
            "    return {'order_id': max(1, order_id), 'ok': True}\n",
            encoding="utf-8",
        )
        res_pass = subprocess.run(
            [sys.executable, "-m", "pytest", str(test_file), "-q"],
            capture_output=True,
            text=True,
        )
        assert res_pass.returncode == 0, res_pass.stdout + res_pass.stderr
    finally:
        ring.close()


def test_state_differ_detects_http_transaction_mutations(tmp_path: Path):
    ring = WalRingBuffer(db_path=str(tmp_path / "differ.sqlite"))
    try:
        differ = StateDiffer(ring)
        path_diff = differ._compare_payload_shapes(
            "http_transaction",
            {"path": "/api/v1/checkout", "body": {"amount": 10}},
            {"path": "/api/v2/checkout", "body": {"amount": 10}},
        )
        assert path_diff is not None
        assert "HTTP target path changed" in path_diff

        key_diff = differ._compare_payload_shapes(
            "http_transaction",
            {"path": "/api/v1/checkout", "body": {"amount": 10, "currency": "USD"}},
            {"path": "/api/v1/checkout", "body": {"amount": 10}},
        )
        assert key_diff is not None
        assert "currency" in key_diff
    finally:
        ring.close()


def test_wal_ring_close_prunes_excess_records_below_batch_threshold(tmp_path: Path):
    db_path = str(tmp_path / "prune_on_close.sqlite")
    ring = WalRingBuffer(db_path=db_path, max_records=10, prune_batch_size=500)
    for i in range(15):
        ring.record("APP_LOG", {"seq": i}, timestamp=1000.0 + i)
    assert ring.count() == 15
    # Closing must trigger _prune_unlocked() even though insert_counter < prune_batch_size
    ring.close()

    reopened = WalRingBuffer(db_path=db_path, max_records=10, prune_batch_size=500)
    try:
        assert reopened.count() <= 10
    finally:
        reopened.close()


def test_version_030_alignment(tmp_path: Path):
    import tomllib
    pyproject_path = Path(__file__).resolve().parent.parent / "pyproject.toml"
    with open(pyproject_path, "rb") as f:
        project_version = tomllib.load(f)["project"]["version"]
    assert blackbox.__version__ == project_version
    server = BlackboxMCPServer(db_path=str(tmp_path / "mcp.sqlite"))
    try:
        assert server.version == blackbox.__version__
    finally:
        server.close()
