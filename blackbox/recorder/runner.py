"""
Genesis Blackbox: Subprocess & Script Execution Runner
Injects flight recorder WAL ring-buffer and exception hook into execution environments.
"""

import os
import sys
import runpy
import shutil
import tempfile
import subprocess
from .wal_ring import WalRingBuffer
from .exception_hook import ExceptionHook
from .process_hook import SubprocessInterceptor


_BOOTSTRAP_SITECUSTOMIZE = """import atexit
import importlib.machinery
import importlib.util
import os
import sys

_bb_dir = os.path.dirname(os.path.abspath(__file__))
_db_path = os.environ.get("BLACKBOX_DB_PATH")
if _db_path and not os.environ.get("_BLACKBOX_CHILD_ATTACHED"):
    os.environ["_BLACKBOX_CHILD_ATTACHED"] = "1"
    try:
        from blackbox.recorder.wal_ring import WalRingBuffer
        from blackbox.recorder.exception_hook import install_exception_hook, uninstall_exception_hook
        from blackbox.recorder.process_hook import SubprocessInterceptor

        _ring = WalRingBuffer(db_path=_db_path)
        install_exception_hook(wal_ring=_ring)
        SubprocessInterceptor.install_global(wal_ring=_ring)

        def _cleanup():
            try:
                uninstall_exception_hook()
                SubprocessInterceptor.uninstall()
                _ring.close()
            except Exception:
                pass

        atexit.register(_cleanup)
    except Exception:
        pass

# Chain any downstream sitecustomize.py present on sys.path
_remaining = [p for p in sys.path if os.path.abspath(p or ".") != _bb_dir]
_spec = importlib.machinery.PathFinder.find_spec("sitecustomize", _remaining)
if _spec and _spec.loader and _spec.origin != __file__:
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
"""


def run_target(cmd_args, db_path: str = ".blackbox/wal.sqlite") -> int:
    """Executes target script or command with automatic Blackbox flight recorder attached."""
    if not cmd_args:
        print("[Genesis-Blackbox] Error: No target command provided.", file=sys.stderr)
        return 1

    ring = WalRingBuffer(db_path=db_path)
    target = cmd_args[0]

    if target.endswith(".py") and os.path.isfile(target):
        hook = ExceptionHook(wal_ring=ring)
        hook.install()
        SubprocessInterceptor.install_global(wal_ring=ring)
        orig_argv = sys.argv[:]
        sys.argv = list(cmd_args)
        ring.record(event_type="PROCESS_START", payload={"script": target, "argv": cmd_args}, source="blackbox_runner")
        try:
            runpy.run_path(target, run_name="__main__")
            ring.record(event_type="PROCESS_EXIT", payload={"script": target, "exit_code": 0}, source="blackbox_runner")
            return 0
        except SystemExit as se:
            code = se.code if isinstance(se.code, int) else (0 if se.code is None else 1)
            ring.record(event_type="PROCESS_EXIT", payload={"script": target, "exit_code": code}, source="blackbox_runner")
            return code
        except BaseException as exc:
            hook.handle_exception(type(exc), exc, exc.__traceback__)
            ring.record(event_type="PROCESS_EXIT", payload={"script": target, "exit_code": 1}, source="blackbox_runner", status="error")
            raise
        finally:
            sys.argv = orig_argv
            hook.uninstall()
            SubprocessInterceptor.uninstall()
            ring.close()

    bootstrap_dir = tempfile.mkdtemp(prefix="blackbox_boot_")
    try:
        site_path = os.path.join(bootstrap_dir, "sitecustomize.py")
        with open(site_path, "w", encoding="utf-8") as f_boot:
            f_boot.write(_BOOTSTRAP_SITECUSTOMIZE)

        env = os.environ.copy()
        env.pop("_BLACKBOX_CHILD_ATTACHED", None)
        env["BLACKBOX_DB_PATH"] = os.path.abspath(db_path)
        pkg_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        existing_pp = env.get("PYTHONPATH", "")
        path_parts = [bootstrap_dir, pkg_dir] + ([existing_pp] if existing_pp else [])
        env["PYTHONPATH"] = os.pathsep.join(path_parts)

        # Prepare Node.js preload hook alongside Python sitecustomize
        from .node_preload import write_node_preload_script, ingest_node_spool
        preload_path = write_node_preload_script(bootstrap_dir)
        spool_path = os.path.join(bootstrap_dir, "blackbox_node_spool.jsonl")
        env["BLACKBOX_NODE_SPOOL"] = spool_path

        existing_node_opts = env.get("NODE_OPTIONS", "")
        req_opt = f"--require {preload_path}"
        env["NODE_OPTIONS"] = f"{req_opt} {existing_node_opts}".strip()

        start_eid = ring.record(event_type="SUBPROCESS_START", payload={"command": cmd_args}, source="blackbox_runner")
        is_node = any(
            cmd_args[0].endswith(x)
            for x in ("node", "npm", "npx", "yarn", "pnpm", "bun", "deno", "tsx", "ts-node")
        ) or any(
            cmd_args[0].endswith(ext)
            for ext in (".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs")
        )

        try:
            if is_node:
                proc = subprocess.run(cmd_args, env=env, capture_output=True, text=True)
                if proc.stdout:
                    sys.stdout.write(proc.stdout)
                    sys.stdout.flush()
                if proc.stderr:
                    sys.stderr.write(proc.stderr)
                    sys.stderr.flush()
                status = "ok" if proc.returncode == 0 else "error"
                ring.record(
                    event_type="SUBPROCESS_EXIT",
                    payload={"command": cmd_args, "returncode": proc.returncode},
                    source="blackbox_runner",
                    status=status
                )
                # Ingest active runtime events captured in Node spool
                ingest_node_spool(spool_path, ring)

                if proc.returncode != 0 and proc.stderr:
                    # Check if an EXCEPTION was already recorded by the preload hook in this run
                    recent_excs = [e for e in ring.query_recent(limit=10, event_type="EXCEPTION") if e.get("id", 0) > start_eid]
                    if not recent_excs:
                        from ..ast_engine.polyglot import parse_v8_stack_trace
                        v8_exc = parse_v8_stack_trace(proc.stderr)
                        if v8_exc:
                            ring.record(
                                event_type="EXCEPTION",
                                payload=v8_exc,
                                source="node_runner",
                                status="error"
                            )
                return proc.returncode
            else:
                proc = subprocess.run(cmd_args, env=env)
                ring.record(
                    event_type="SUBPROCESS_EXIT",
                    payload={"command": cmd_args, "returncode": proc.returncode},
                    source="blackbox_runner",
                    status="ok" if proc.returncode == 0 else "error"
                )
                ingest_node_spool(spool_path, ring)
                return proc.returncode
        except Exception as e:
            ring.record(
                event_type="SUBPROCESS_ERROR",
                payload={"command": cmd_args, "error": str(e)},
                source="blackbox_runner",
                status="error"
            )
            print(f"[Genesis-Blackbox] Execution failed: {e}", file=sys.stderr)
            return 1
    finally:
        ring.close()
        shutil.rmtree(bootstrap_dir, ignore_errors=True)
