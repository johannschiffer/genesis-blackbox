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

        ring.record(event_type="SUBPROCESS_START", payload={"command": cmd_args}, source="blackbox_runner")
        try:
            proc = subprocess.run(cmd_args, env=env)
            ring.record(
                event_type="SUBPROCESS_EXIT",
                payload={"command": cmd_args, "returncode": proc.returncode},
                source="blackbox_runner",
                status="ok" if proc.returncode == 0 else "error"
            )
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
