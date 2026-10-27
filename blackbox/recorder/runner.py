"""
Genesis Blackbox: Subprocess & Script Execution Runner
Injects flight recorder WAL ring-buffer and exception hook into execution environments.
"""

import os
import sys
import runpy
import subprocess
from .wal_ring import WalRingBuffer
from .exception_hook import ExceptionHook


def run_target(cmd_args, db_path: str = ".blackbox/wal.sqlite") -> int:
    """Executes target script or command with automatic Blackbox flight recorder attached."""
    if not cmd_args:
        print("[Genesis-Blackbox] Error: No target command provided.", file=sys.stderr)
        return 1

    ring = WalRingBuffer(db_path=db_path)
    hook = ExceptionHook(wal_ring=ring)
    hook.install()

    # If first arg is a python script existing on disk
    target = cmd_args[0]
    if target.endswith(".py") and os.path.isfile(target):
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
        except BaseException:
            # sys.excepthook will catch and serialize to WAL
            raise

    # Otherwise execute as subprocess with environment variables
    env = os.environ.copy()
    env["BLACKBOX_DB_PATH"] = os.path.abspath(db_path)
    # Ensure current python environment and packages are in PYTHONPATH
    pkg_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{pkg_dir}:{existing_pp}" if existing_pp else pkg_dir

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
