"""
Genesis Blackbox Native Pytest Plugin
Hooks into pytest execution to record deep execution traces, local variable states,
and AST blast-radius causal crash autopsies without test code modification.
"""

import os
import sys
import time
import pytest
from typing import Optional

from .recorder.wal_ring import WalRingBuffer
from .recorder.exception_hook import ExceptionHook, install_exception_hook, uninstall_exception_hook
from .recorder.process_hook import SubprocessInterceptor
from .compiler.autopsy import CausalCrashCompiler
from .compiler.reproducer import CrashReproducer
from .ast_engine.graph import CodebaseGraph
from .ci.github_summary import export_github_step_summary


def pytest_addoption(parser):
    """Adds command-line options for Genesis Blackbox flight recording."""
    group = parser.getgroup("blackbox", "Genesis Blackbox Runtime Flight Recorder")
    group.addoption(
        "--blackbox",
        action="store_true",
        default=False,
        dest="blackbox_enabled",
        help="Enable Genesis Blackbox causal crash flight recorder and autopsy generation."
    )
    group.addoption(
        "--blackbox-db",
        action="store",
        default=".blackbox/wal.sqlite",
        dest="blackbox_db",
        help="Path to circular SQLite WAL ring-buffer database (default: .blackbox/wal.sqlite)."
    )


def pytest_configure(config):
    """Initializes WalRingBuffer and runtime interceptors if --blackbox is active."""
    config.addinivalue_line(
        "markers", "blackbox: mark test to record runtime flight traces with Genesis Blackbox"
    )

    if not config.getoption("blackbox_enabled", False):
        return

    db_path = config.getoption("blackbox_db", ".blackbox/wal.sqlite")
    wal_ring = WalRingBuffer(db_path=db_path)
    config._blackbox_wal = wal_ring

    # Install subprocess execution interceptor
    SubprocessInterceptor.install_global(wal_ring=wal_ring)

    # Install exception hook
    install_exception_hook(wal_ring=wal_ring)


def pytest_unconfigure(config):
    """Restores global runtime hooks and closes WAL connection upon test session completion."""
    if hasattr(config, "_blackbox_wal"):
        uninstall_exception_hook()
        SubprocessInterceptor.uninstall()
        try:
            config._blackbox_wal.close()
        except Exception:
            pass
        delattr(config, "_blackbox_wal")
    if hasattr(config, "_blackbox_graph"):
        delattr(config, "_blackbox_graph")



@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    """Records test initiation and completion boundaries in the WAL ring-buffer."""
    config = item.config
    wal_ring = getattr(config, "_blackbox_wal", None)
    if not wal_ring:
        yield
        return

    markers = [m.name for m in item.iter_markers()]
    doc = item.obj.__doc__ if hasattr(item, "obj") and item.obj else ""

    wal_ring.record(
        event_type="TEST_START",
        payload={
            "nodeid": item.nodeid,
            "name": item.name,
            "docstring": doc.strip() if doc else "",
            "markers": markers
        },
        source="pytest_plugin",
        status="running"
    )

    t0 = time.time()
    outcome = yield
    duration_ms = (time.time() - t0) * 1000.0

    wal_ring.record(
        event_type="TEST_END",
        payload={
            "nodeid": item.nodeid,
            "name": item.name
        },
        source="pytest_plugin",
        duration_ms=duration_ms,
        status="completed"
    )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Captures unhandled test failures, generates .blackbox.md, synthesizes reproduction script, and prints banner."""
    outcome = yield
    report = outcome.get_result()

    config = item.config
    wal_ring = getattr(config, "_blackbox_wal", None)
    if not wal_ring:
        return

    # Process failure during test execution phase
    if report.failed and call.excinfo is not None:
        exc_type = call.excinfo.type
        exc_value = call.excinfo.value
        tb = getattr(call.excinfo, "_excinfo", (None, None, None))[2] or getattr(call.excinfo, "tb", None)

        # 1. Deep serialization into WAL
        hook = ExceptionHook(wal_ring=wal_ring)
        exc_payload = hook.handle_exception(exc_type, exc_value, tb)
        exc_payload["test_nodeid"] = item.nodeid
        exc_payload["test_name"] = item.name

        # 2. Compile causal autopsy capsule
        compiler = CausalCrashCompiler(wal_ring=wal_ring)
        capsule = compiler.compile(exception_data=exc_payload)
        autopsy_path = ".blackbox.md"
        try:
            with open(autopsy_path, "w", encoding="utf-8") as f:
                f.write(capsule)
        except Exception:
            pass

        # 3. Generate reproduction test script
        reproducer = CrashReproducer(wal_ring=wal_ring)
        reproduce_code = reproducer.generate()
        reproduce_path = "test_blackbox_reproduce.py"
        try:
            with open(reproduce_path, "w", encoding="utf-8") as f:
                f.write(reproduce_code)
        except Exception:
            pass

        blast_summary = "N/A"
        root_cause = exc_payload.get("root_cause") or {}
        target_file = root_cause.get("filename")
        target_fn = root_cause.get("function")
        if target_file and target_fn and os.path.exists(target_file):
            try:
                graph = getattr(config, "_blackbox_graph", None)
                if graph is None:
                    graph = CodebaseGraph()
                    graph.parse_directory(os.getcwd(), max_files=100)
                    config._blackbox_graph = graph
                blast = graph.get_blast_radius(target_function=target_fn, file_path=target_file)
                callers = len(blast.get("upstream_callers", []))
                callees = len(blast.get("downstream_callees", []))
                blast_summary = f"{callers} upstream callers, {callees} downstream callees"
            except Exception:
                pass

        # 5. Export to GitHub Step Summary if active in CI environment
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            try:
                export_github_step_summary(autopsy_path=autopsy_path)
            except Exception:
                pass

        # 6. Emit clean terminal banner
        print("\n" + "=" * 65)
        print(f"[GENESIS-BLACKBOX] Test Failure Captured: {item.nodeid}")
        print(f" > Autopsy Capsule: {autopsy_path}")
        print(f" > Reproducer Test: {reproduce_path}")
        print(f" > AST Blast Radius: {blast_summary}")
        print("=" * 65 + "\n")
