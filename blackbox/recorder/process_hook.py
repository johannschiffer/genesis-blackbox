"""
Genesis Blackbox: Subprocess Interceptor & Flight Recorder Hook
Transparently instruments subprocess.Popen and subprocess.run to capture command invocations,
arguments (with credential and secret redaction), working directories, exit codes, and latencies.
"""

import os
import sys
import time
import shlex
import inspect
import threading
import subprocess
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from .wal_ring import WalRingBuffer

SENSITIVE_ARG_KEYWORDS = {
    "token", "password", "passwd", "pass", "secret", "key", "api_key",
    "api-key", "auth", "credential", "private", "access_token"
}


def sanitize_arg_value(arg: Any) -> str:
    """Sanitizes an individual argument string, redacting tokens and secrets."""
    if not isinstance(arg, str):
        arg = str(arg)

    # Redact URL credentials: http://user:password@host -> http://user:[REDACTED]@host
    if "://" in arg and "@" in arg:
        try:
            proto, rest = arg.split("://", 1)
            userinfo, host = rest.split("@", 1)
            if ":" in userinfo:
                u, _ = userinfo.split(":", 1)
                arg = f"{proto}://{u}:[REDACTED]@{host}"
            else:
                arg = f"{proto}://[REDACTED]@{host}"
        except Exception:
            pass

    # Redact common token prefixes
    for prefix in ("ghp_", "gho_", "github_pat_", "pypi-", "sk-", "xoxb-", "xoxp-"):
        if prefix in arg:
            idx = arg.find(prefix)
            arg = arg[:idx] + "[REDACTED_SECRET]"

    # Handle key=value style arguments
    if "=" in arg:
        key, val = arg.split("=", 1)
        k_lower = key.lstrip("-").lower().replace("-", "_")
        if any(s in k_lower for s in SENSITIVE_ARG_KEYWORDS):
            return f"{key}=[REDACTED]"

    return arg


def sanitize_command_and_args(args: Any) -> Tuple[str, Any]:
    """
    Sanitizes command arguments, returning (command_name, sanitized_args).
    Redacts sensitive flags (e.g., --token <secret> or --password=<secret>).
    """
    if args is None:
        return ("", "")

    if isinstance(args, (list, tuple)):
        sanitized_list: List[str] = []
        skip_next = False
        cmd_name = str(args[0]) if len(args) > 0 else ""

        for i, item in enumerate(args):
            if skip_next:
                sanitized_list.append("[REDACTED]")
                skip_next = False
                continue

            item_str = str(item)
            item_clean = item_str.lstrip("-").lower().replace("-", "_")

            # Check if this flag expects a sensitive parameter next
            if any(s == item_clean for s in SENSITIVE_ARG_KEYWORDS):
                sanitized_list.append(item_str)
                skip_next = True
            else:
                sanitized_list.append(sanitize_arg_value(item_str))

        return (cmd_name, sanitized_list)

    if isinstance(args, str):
        cmd_str = args.strip()
        parts = cmd_str.split()
        cmd_name = parts[0] if parts else ""

        try:
            tokens = shlex.split(cmd_str)
            _, sanitized_tokens = sanitize_command_and_args(tokens)
            return (cmd_name, " ".join(sanitized_tokens))
        except Exception:
            # Fallback simple sanitization
            return (cmd_name, sanitize_arg_value(cmd_str))

    return (str(args), str(args))


def truncate_stderr(stderr_val: Any, max_len: int = 1000) -> Optional[str]:
    """Decodes and bounds stderr output to a safe length for error autopsies."""
    if stderr_val is None:
        return None
    if isinstance(stderr_val, (bytes, bytearray)):
        text = stderr_val.decode("utf-8", errors="replace")
    else:
        text = str(stderr_val)
    text = text.strip()
    if len(text) > max_len:
        return text[:max_len] + f"... [truncated {len(text)} chars]"
    return text if text else None


class SubprocessInterceptor:
    """
    Transparent execution interceptor for subprocess.Popen and subprocess.run.
    Records command lines, exit codes, execution durations, and error outputs
    directly into a circular WAL ring-buffer.
    """

    _tls = threading.local()
    _installed = False
    _original_run = None
    _original_popen = None
    _global_instance: Optional["SubprocessInterceptor"] = None

    def __init__(self, wal_ring: Optional[WalRingBuffer] = None):
        if wal_ring is None:
            wal_ring = WalRingBuffer(db_path=".blackbox/wal.sqlite")
        self.wal_ring = wal_ring

    def __enter__(self) -> "SubprocessInterceptor":
        self.install()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.uninstall()

    def record_subprocess_event(
        self,
        command: str,
        sanitized_args: Any,
        cwd: Optional[str],
        start_ts: float,
        duration_ms: float,
        returncode: int,
        stderr: Optional[str] = None
    ) -> None:
        """Writes SUBPROCESS event into the WAL ring-buffer."""
        status = "ok" if returncode == 0 else "error"
        payload = {
            "command": command,
            "args": sanitized_args,
            "cwd": cwd,
            "start_time": start_ts,
            "returncode": returncode,
            "status": status,
            "stderr": stderr if status == "error" else None
        }

        try:
            self.wal_ring.record(
                event_type="SUBPROCESS",
                source="subprocess",
                duration_ms=round(duration_ms, 3),
                status=status,
                payload=payload
            )
        except Exception as e:
            print(f"[Genesis-Blackbox] Failed to record SUBPROCESS event: {e}", file=sys.stderr)

    def install(self) -> None:
        """Installs the monkeypatch on subprocess.Popen and subprocess.run."""
        if SubprocessInterceptor._installed:
            SubprocessInterceptor._global_instance = self
            return

        SubprocessInterceptor._original_run = subprocess.run
        SubprocessInterceptor._original_popen = subprocess.Popen
        SubprocessInterceptor._global_instance = self

        interceptor = self

        def patched_run(*args, **kwargs):
            active = SubprocessInterceptor._global_instance or interceptor
            cmd_args = args[0] if args else kwargs.get("args")
            cwd = kwargs.get("cwd") or os.getcwd()
            cmd_name, clean_args = sanitize_command_and_args(cmd_args)

            start_ts = time.time()
            start_perf = time.perf_counter()

            # Mark in_run in thread-local to prevent duplicate Popen recording
            SubprocessInterceptor._tls.in_run = True
            try:
                result = SubprocessInterceptor._original_run(*args, **kwargs)
                duration_ms = (time.perf_counter() - start_perf) * 1000.0
                err_text = None
                if result.returncode != 0:
                    err_text = truncate_stderr(getattr(result, "stderr", None))

                active.record_subprocess_event(
                    command=cmd_name,
                    sanitized_args=clean_args,
                    cwd=str(cwd),
                    start_ts=start_ts,
                    duration_ms=duration_ms,
                    returncode=result.returncode,
                    stderr=err_text
                )
                return result
            except subprocess.CalledProcessError as cpe:
                duration_ms = (time.perf_counter() - start_perf) * 1000.0
                err_text = truncate_stderr(cpe.stderr or str(cpe))
                active.record_subprocess_event(
                    command=cmd_name,
                    sanitized_args=clean_args,
                    cwd=str(cwd),
                    start_ts=start_ts,
                    duration_ms=duration_ms,
                    returncode=cpe.returncode,
                    stderr=err_text
                )
                raise
            except Exception as exc:
                duration_ms = (time.perf_counter() - start_perf) * 1000.0
                active.record_subprocess_event(
                    command=cmd_name,
                    sanitized_args=clean_args,
                    cwd=str(cwd),
                    start_ts=start_ts,
                    duration_ms=duration_ms,
                    returncode=-1,
                    stderr=str(exc)
                )
                raise
            finally:
                SubprocessInterceptor._tls.in_run = False

        orig_popen_class = SubprocessInterceptor._original_popen

        class TracedPopen(orig_popen_class):
            def __init__(self, *p_args, **p_kwargs):
                self._bb_start_ts = time.time()
                self._bb_start_perf = time.perf_counter()
                cmd_args = p_args[0] if p_args else p_kwargs.get("args")
                self._bb_cwd = p_kwargs.get("cwd") or os.getcwd()
                self._bb_cmd_name, self._bb_clean_args = sanitize_command_and_args(cmd_args)
                self._bb_recorded = False
                self._bb_in_run = getattr(SubprocessInterceptor._tls, "in_run", False)

                super().__init__(*p_args, **p_kwargs)

            def _record_exit(self, retcode: int, stderr_val: Any = None):
                if self._bb_recorded or self._bb_in_run:
                    return
                self._bb_recorded = True
                active = SubprocessInterceptor._global_instance or interceptor
                dur_ms = (time.perf_counter() - self._bb_start_perf) * 1000.0
                err_text = truncate_stderr(stderr_val) if retcode != 0 else None
                active.record_subprocess_event(
                    command=self._bb_cmd_name,
                    sanitized_args=self._bb_clean_args,
                    cwd=str(self._bb_cwd),
                    start_ts=self._bb_start_ts,
                    duration_ms=dur_ms,
                    returncode=retcode,
                    stderr=err_text
                )

            def poll(self):
                code = super().poll()
                if code is not None:
                    self._record_exit(code)
                return code

            def wait(self, timeout=None):
                code = super().wait(timeout=timeout)
                self._record_exit(code)
                return code

            def communicate(self, input=None, timeout=None):
                stdout, stderr = super().communicate(input=input, timeout=timeout)
                self._record_exit(self.returncode, stderr_val=stderr)
                return stdout, stderr

            def __exit__(self, exc_type, exc_val, exc_tb):
                super().__exit__(exc_type, exc_val, exc_tb)
                self._record_exit(self.returncode if self.returncode is not None else 0)

        subprocess.run = patched_run
        subprocess.Popen = TracedPopen
        SubprocessInterceptor._installed = True

    @classmethod
    def install_global(cls, wal_ring: Optional[WalRingBuffer] = None) -> "SubprocessInterceptor":
        """Class method to globally install the subprocess interceptor."""
        inst = cls(wal_ring=wal_ring)
        inst.install()
        return inst

    @classmethod
    def uninstall(cls) -> None:
        """Restores original subprocess.run and subprocess.Popen implementations."""
        if not cls._installed:
            return

        if cls._original_run:
            subprocess.run = cls._original_run
            cls._original_run = None

        if cls._original_popen:
            subprocess.Popen = cls._original_popen
            cls._original_popen = None

        cls._installed = False
        cls._global_instance = None
