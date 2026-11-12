"""
Genesis Blackbox: Zero-Dependency Resource & Memory Profiler
Measures heap allocations (`tracemalloc`), GC state, and open file descriptors (`/proc/self/fd`
on Linux with safe cross-platform fallback) and records `RESOURCE_PROFILE` events to the WAL.
"""

import gc
import os
import tracemalloc
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from .wal_ring import WalRingBuffer


def _count_open_fds() -> Optional[int]:
    """Counts open file descriptors via `/proc/self/fd` on Linux, returning None on unsupported platforms."""
    fd_dir = "/proc/self/fd"
    try:
        if os.path.isdir(fd_dir):
            return len(os.listdir(fd_dir))
    except OSError:
        return None
    return None


def _extract_top_allocations(snapshot: Optional[tracemalloc.Snapshot], limit: int = 5) -> List[Dict[str, Any]]:
    if snapshot is None:
        return []
    entries: List[Dict[str, Any]] = []
    try:
        stats = snapshot.statistics("lineno")[:limit]
    except Exception:
        return []

    for stat in stats:
        frame = stat.traceback[0] if stat.traceback else None
        line_no = int(frame.lineno) if frame else 0
        entries.append({
            "file": frame.filename if frame else "<unknown>",
            "line": line_no,
            "lineno": line_no,
            "size_kb": round(stat.size / 1024.0, 3),
            "count": int(stat.count),
        })
    return entries


class ResourceProfiler:
    """
    100% stdlib memory and file-descriptor profiler using `tracemalloc`, `gc`, and `/proc/self/fd`.
    """

    DEFAULT_PEAK_THRESHOLD_KB: float = 65536.0
    DEFAULT_DELTA_THRESHOLD_KB: float = 16384.0

    def __init__(
        self,
        peak_threshold_kb: float = DEFAULT_PEAK_THRESHOLD_KB,
        delta_threshold_kb: float = DEFAULT_DELTA_THRESHOLD_KB,
    ):
        self.peak_threshold_kb = float(peak_threshold_kb)
        self.delta_threshold_kb = float(delta_threshold_kb)

    def snapshot(self, top_n: int = 5) -> Dict[str, Any]:
        """Captures an instantaneous memory, GC, and file-descriptor snapshot."""
        tracing = tracemalloc.is_tracing()
        if tracing:
            current_bytes, peak_bytes = tracemalloc.get_traced_memory()
            top_allocations = _extract_top_allocations(tracemalloc.take_snapshot(), limit=top_n)
        else:
            current_bytes, peak_bytes = 0, 0
            top_allocations = []

        current_kb = round(current_bytes / 1024.0, 3)
        peak_kb = round(peak_bytes / 1024.0, 3)
        return {
            "current_kb": current_kb,
            "peak_kb": peak_kb,
            "delta_kb": 0.0,
            "open_fds": _count_open_fds(),
            "gc_counts": list(gc.get_count()),
            "top_allocations": top_allocations,
        }

    @contextmanager
    def profile_scope(
        self,
        wal_ring: Optional[WalRingBuffer] = None,
        label: str = "run",
    ) -> Iterator[Dict[str, Any]]:
        """
        Profiles memory and descriptor usage across a block and records a
        `RESOURCE_PROFILE` event into the WAL upon exit.
        """
        was_tracing = tracemalloc.is_tracing()
        if not was_tracing:
            tracemalloc.start(10)

        start_current, _ = tracemalloc.get_traced_memory()
        if not was_tracing:
            tracemalloc.reset_peak()

        metrics: Dict[str, Any] = {"label": label}
        try:
            yield metrics
        finally:
            end_current, end_peak = tracemalloc.get_traced_memory()
            snap = tracemalloc.take_snapshot()
            top_allocations = _extract_top_allocations(snap, limit=5)

            current_kb = round(end_current / 1024.0, 3)
            peak_kb = round(end_peak / 1024.0, 3)
            delta_kb = round((end_current - start_current) / 1024.0, 3)
            threshold_exceeded = (
                peak_kb >= self.peak_threshold_kb
                or delta_kb >= self.delta_threshold_kb
            )

            metrics.update({
                "label": label,
                "current_kb": current_kb,
                "peak_kb": peak_kb,
                "delta_kb": delta_kb,
                "open_fds": _count_open_fds(),
                "gc_counts": list(gc.get_count()),
                "top_allocations": top_allocations,
                "peak_threshold_kb": self.peak_threshold_kb,
                "delta_threshold_kb": self.delta_threshold_kb,
                "threshold_exceeded": threshold_exceeded,
            })

            if not was_tracing:
                tracemalloc.stop()

            if wal_ring is not None:
                wal_ring.record(
                    event_type="RESOURCE_PROFILE",
                    payload=dict(metrics),
                    source="resource_profiler",
                    status="warning" if threshold_exceeded else "ok",
                )
