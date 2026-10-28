"""
Genesis Blackbox: Deep Frame & Local Variable Exception Serializer
Captures rich runtime local variable states across call stacks with
circular-reference protection, depth capping, and automated secret sanitization.
"""

import sys
import types
import threading
import traceback
import inspect
from collections.abc import Mapping
from typing import Dict, Any, List, Optional, Set


# Common sensitive keywords to sanitize
SENSITIVE_KEYS = {
    "password", "secret", "token", "auth", "authorization",
    "key", "api_key", "cookie", "credential", "private", "access_token"
}


class SafeSerializer:
    """
    Serializes arbitrary Python objects and local variable scopes
    with circular reference protection, depth bounds, and PII redaction.
    """

    def __init__(
        self,
        max_depth: int = 3,
        max_str_len: int = 500,
        max_items: int = 30
    ):
        self.max_depth = max_depth
        self.max_str_len = max_str_len
        self.max_items = max_items

    def serialize(self, obj: Any, seen: Optional[Set[int]] = None, depth: int = 0) -> Any:
        if seen is None:
            seen = set()

        if depth > self.max_depth:
            return "<depth-limit-reached>"

        obj_id = id(obj)
        if obj_id in seen:
            return "<circular-reference>"

        # Primitive types
        if obj is None or isinstance(obj, (bool, int, float)):
            return obj

        if isinstance(obj, str):
            if len(obj) > self.max_str_len:
                return obj[:self.max_str_len] + f"... ({len(obj)} chars)"
            return obj

        if isinstance(obj, (bytes, bytearray)):
            return f"<bytes: {len(obj)} bytes>"

        # Collections (track visited objects)
        seen.add(obj_id)
        try:
            if isinstance(obj, (list, tuple, set)):
                items = list(obj)[:self.max_items]
                serialized = [self.serialize(x, seen.copy(), depth + 1) for x in items]
                if len(obj) > self.max_items:
                    serialized.append(f"... +{len(obj) - self.max_items} more items")
                return serialized

            if isinstance(obj, (dict, Mapping)):
                serialized_dict = {}
                count = 0
                for k, v in obj.items():
                    if count >= self.max_items:
                        serialized_dict["..."] = f"+{len(obj) - self.max_items} more keys"
                        break

                    key_str = str(k)
                    # Check if sensitive key
                    if any(s in key_str.lower() for s in SENSITIVE_KEYS):
                        serialized_dict[key_str] = "******** [REDACTED]"
                    else:
                        serialized_dict[key_str] = self.serialize(v, seen.copy(), depth + 1)
                    count += 1
                return serialized_dict

            # Inspectable custom objects
            if hasattr(obj, "__dict__"):
                attrs = {}
                count = 0
                for k, v in vars(obj).items():
                    if k.startswith("_"):
                        continue
                    if count >= self.max_items:
                        attrs["..."] = f"+{len(vars(obj)) - self.max_items} more"
                        break

                    if any(s in str(k).lower() for s in SENSITIVE_KEYS):
                        attrs[k] = "******** [REDACTED]"
                    else:
                        attrs[k] = self.serialize(v, seen.copy(), depth + 1)
                    count += 1

                cls_name = obj.__class__.__name__
                return {"__class__": cls_name, "attributes": attrs}

            # Fallback for callables, generators, modules, file handles
            rep = repr(obj)
            if len(rep) > self.max_str_len:
                rep = rep[:self.max_str_len] + "..."
            return rep

        finally:
            seen.discard(obj_id)


class ExceptionHook:
    """
    Deep runtime exception handler.
    Extracts stack frames, local variables, and context lines at crash time.
    """

    def __init__(
        self,
        wal_ring=None,
        serializer: Optional[SafeSerializer] = None
    ):
        self.wal_ring = wal_ring
        self.serializer = serializer or SafeSerializer()
        self._original_excepthook = None
        self._original_threading_excepthook = None

    def serialize_exception(
        self,
        exc_type,
        exc_value,
        tb,
        attach_locals: bool = True
    ) -> Dict[str, Any]:
        """
        Extracts complete stack information including local variable scopes.
        """
        frames: List[Dict[str, Any]] = []
        curr_tb = tb

        while curr_tb:
            frame = curr_tb.tb_frame
            lineno = curr_tb.tb_lineno
            code = frame.f_code
            func_name = code.co_name
            filename = code.co_filename

            frame_info: Dict[str, Any] = {
                "filename": filename,
                "lineno": lineno,
                "function": func_name,
            }

            # Extract source context line if available
            try:
                lines, start_line = inspect.getsourcelines(code)
                rel_idx = lineno - start_line
                if 0 <= rel_idx < len(lines):
                    frame_info["code_line"] = lines[rel_idx].strip()
            except Exception:
                frame_info["code_line"] = None

            # Deep inspect local variables
            if attach_locals:
                raw_locals = frame.f_locals
                frame_info["locals"] = self.serializer.serialize(raw_locals)

            frames.append(frame_info)
            curr_tb = curr_tb.tb_next

        formatted_tb = "".join(traceback.format_exception(exc_type, exc_value, tb))

        # Identify likely root-cause frame (innermost non-standard-library frame)
        root_cause_frame = None
        for f in reversed(frames):
            fname = f["filename"]
            if not any(lib in fname for lib in ["lib/python", "site-packages", "/usr/"]):
                root_cause_frame = f
                break

        if not root_cause_frame and frames:
            root_cause_frame = frames[-1]

        return {
            "exception_type": getattr(exc_type, "__name__", str(exc_type)),
            "exception_message": str(exc_value),
            "traceback": formatted_tb,
            "total_frames": len(frames),
            "frames": frames,
            "root_cause": root_cause_frame
        }

    def handle_exception(self, exc_type, exc_value, tb) -> Dict[str, Any]:
        """Processes exception and writes to WAL ring-buffer if configured."""
        payload = self.serialize_exception(exc_type, exc_value, tb)

        if self.wal_ring is not None:
            try:
                self.wal_ring.record(
                    event_type="EXCEPTION",
                    payload=payload,
                    source="exception_hook",
                    status="error"
                )
            except Exception as e:
                print(f"[Genesis-Blackbox] Failed to record exception: {e}", file=sys.stderr)

        return payload

    def install(self) -> None:
        """Installs as the global sys.excepthook and threading.excepthook."""
        self._original_excepthook = sys.excepthook

        def _hook(exc_type, exc_value, tb):
            self.handle_exception(exc_type, exc_value, tb)
            if self._original_excepthook:
                self._original_excepthook(exc_type, exc_value, tb)

        sys.excepthook = _hook

        if hasattr(threading, "excepthook"):
            self._original_threading_excepthook = threading.excepthook

            def _thread_hook(args):
                self.handle_exception(args.exc_type, args.exc_value, args.exc_traceback)
                if self._original_threading_excepthook:
                    self._original_threading_excepthook(args)

            threading.excepthook = _thread_hook

    def uninstall(self) -> None:
        """Restores original sys.excepthook and threading.excepthook."""
        if self._original_excepthook:
            sys.excepthook = self._original_excepthook
            self._original_excepthook = None
        if self._original_threading_excepthook and hasattr(threading, "excepthook"):
            threading.excepthook = self._original_threading_excepthook
            self._original_threading_excepthook = None


_active_exception_hook: Optional[ExceptionHook] = None


def install_exception_hook(wal_ring: Optional[Any] = None) -> ExceptionHook:
    """Installs global exception hook with the given or default WalRingBuffer."""
    global _active_exception_hook
    if _active_exception_hook is not None:
        if wal_ring is not None:
            _active_exception_hook.wal_ring = wal_ring
        return _active_exception_hook
    hook = ExceptionHook(wal_ring=wal_ring)
    hook.install()
    _active_exception_hook = hook
    return hook


def uninstall_exception_hook() -> None:
    """Restores original sys.excepthook if installed."""
    global _active_exception_hook
    if _active_exception_hook is not None:
        _active_exception_hook.uninstall()
        _active_exception_hook = None

