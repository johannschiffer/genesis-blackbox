"""
Genesis Blackbox: Automatic Sanitized Baseline Manager
Atomically promotes clean WAL execution runs to .blackbox/wal.baseline.sqlite
via sqlite3.Connection.backup(), and compiles automatic baseline divergence reports
when subsequent runs fail.
"""

import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from .wal_ring import WalRingBuffer


class BaselineManager:
    """Manages atomic SQLite WAL baseline promotion and automatic crash divergence compilation."""

    @staticmethod
    def default_baseline_path(wal_path: str = ".blackbox/wal.sqlite") -> str:
        """Derives the sibling baseline SQLite path for a given WAL database path."""
        if not wal_path or wal_path == ":memory:":
            return ".blackbox/wal.baseline.sqlite"
        if os.path.normpath(wal_path) == os.path.normpath(".blackbox/wal.sqlite"):
            return ".blackbox/wal.baseline.sqlite"
        stem, ext = os.path.splitext(wal_path)
        return f"{stem}.baseline{ext or '.sqlite'}"

    @staticmethod
    def default_project_dir(wal_path: str = ".blackbox/wal.sqlite") -> str:
        """Resolves the project directory corresponding to a WAL path."""
        if not wal_path or wal_path == ":memory:":
            return os.getcwd()
        wal_dir = os.path.dirname(os.path.abspath(wal_path))
        if os.path.basename(wal_dir) == ".blackbox":
            return os.path.dirname(wal_dir)
        return wal_dir

    @staticmethod
    def get_latest_event_id(wal_path: str) -> int:
        """Returns the current maximum event ID in the WAL database, or 0 if unavailable."""
        if not wal_path or wal_path == ":memory:" or not os.path.isfile(wal_path):
            return 0
        conn = None
        try:
            conn = sqlite3.connect(wal_path, timeout=5.0)
            row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM blackbox_events;").fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        except sqlite3.Error:
            return 0
        finally:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass

    @staticmethod
    def has_exception_events(wal_path: str, since_event_id: int = 0) -> bool:
        """Checks whether the WAL contains any EXCEPTION events after since_event_id."""
        if not wal_path or wal_path == ":memory:" or not os.path.isfile(wal_path):
            return False
        conn = None
        try:
            conn = sqlite3.connect(wal_path, timeout=5.0)
            row = conn.execute(
                "SELECT COUNT(*) FROM blackbox_events WHERE event_type = 'EXCEPTION' AND id > ?;",
                (int(since_event_id),),
            ).fetchone()
            return bool(row and row[0] > 0)
        except sqlite3.Error:
            return False
        finally:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass

    @staticmethod
    def promote_to_baseline(
        wal_path: str = ".blackbox/wal.sqlite",
        baseline_path: str = ".blackbox/wal.baseline.sqlite",
        since_event_id: int = 0,
    ) -> bool:
        """
        Atomically clones wal_path to baseline_path using sqlite3.Connection.backup()
        when the WAL exists and has 0 EXCEPTION events in the target run window.
        """
        if not wal_path or wal_path == ":memory:" or not os.path.isfile(wal_path):
            return False
        if not baseline_path or baseline_path == ":memory:":
            return False

        if BaselineManager.has_exception_events(wal_path, since_event_id=since_event_id):
            return False

        abs_wal = os.path.abspath(wal_path)
        abs_baseline = os.path.abspath(baseline_path)
        baseline_dir = os.path.dirname(abs_baseline) or "."
        os.makedirs(baseline_dir, exist_ok=True)

        fd, tmp_path = tempfile.mkstemp(prefix=".blackbox_baseline_", suffix=".sqlite", dir=baseline_dir)
        os.close(fd)

        src_conn: Optional[sqlite3.Connection] = None
        dst_conn: Optional[sqlite3.Connection] = None
        try:
            src_conn = sqlite3.connect(abs_wal, timeout=10.0)
            dst_conn = sqlite3.connect(tmp_path, timeout=10.0)
            src_conn.backup(dst_conn)
            dst_conn.commit()
            dst_conn.close()
            dst_conn = None
            src_conn.close()
            src_conn = None
            os.replace(tmp_path, abs_baseline)
            return True
        except Exception:
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass
            return False
        finally:
            if dst_conn is not None:
                try:
                    dst_conn.close()
                except sqlite3.Error:
                    pass
            if src_conn is not None:
                try:
                    src_conn.close()
                except sqlite3.Error:
                    pass

    @staticmethod
    def write_baseline_divergence(
        wal_path: str = ".blackbox/wal.sqlite",
        baseline_path: Optional[str] = None,
        autopsy_path: str = ".blackbox.md",
        diff_output_path: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        If baseline_path exists, diffs failing wal_path against baseline_path via StateDiffer,
        writes .blackbox-diff.md, and appends `## Automatic Baseline Divergence` to autopsy_path.
        """
        target_baseline = baseline_path or BaselineManager.default_baseline_path(wal_path)
        if not target_baseline or not os.path.isfile(target_baseline):
            return None
        if not wal_path or wal_path == ":memory:" or not os.path.isfile(wal_path):
            return None

        from ..compiler.state_differ import StateDiffer

        differ = StateDiffer(db_path=wal_path)
        try:
            diff_res = differ.diff_against_baseline(target_baseline)
            diff_md = differ.compile_markdown(diff_res)
        except Exception:
            return None
        finally:
            differ.close()

        autopsy_dir = os.path.dirname(os.path.abspath(autopsy_path)) or "."
        target_diff_path = diff_output_path or os.path.join(autopsy_dir, ".blackbox-diff.md")
        os.makedirs(os.path.dirname(os.path.abspath(target_diff_path)) or ".", exist_ok=True)
        with open(target_diff_path, "w", encoding="utf-8") as f_diff:
            f_diff.write(diff_md)

        # Build concise summary block for .blackbox.md
        div = diff_res.get("divergence_point")
        b_run = diff_res.get("baseline_run", {})
        f_run = diff_res.get("failing_run", {})
        section_lines = [
            "## Automatic Baseline Divergence",
            "",
            f"- **Baseline Snapshot:** `{target_baseline}` (Run `{b_run.get('run_id', 'baseline')}`)",
            f"- **Failing Run:** `{f_run.get('run_id', 'failing')}`",
            f"- **Full Diff Report:** `{target_diff_path}`",
        ]
        if div:
            section_lines.append(
                f"- **Divergence Point:** Step `#{div.get('step')}` (`{div.get('type')}`) — {div.get('description')}"
            )
        else:
            section_lines.append("- **Divergence Point:** No structural event divergence detected prior to crash.")

        sql_diffs = [s for s in diff_res.get("sql_diff", []) if s.get("divergence")]
        if sql_diffs:
            section_lines.append(f"- **SQL Divergences:** {len(sql_diffs)} query/param shift(s) detected.")
        http_diffs = [h for h in diff_res.get("http_diff", []) if h.get("divergence")]
        if http_diffs:
            section_lines.append(f"- **HTTP Divergences:** {len(http_diffs)} transaction shift(s) detected.")

        section_block = "\n".join(section_lines) + "\n"

        autopsy_file = Path(autopsy_path)
        if autopsy_file.is_file():
            existing = autopsy_file.read_text(encoding="utf-8")
            marker = "## Automatic Baseline Divergence"
            if marker in existing:
                prefix = existing.split(marker, 1)[0].rstrip()
                updated = f"{prefix}\n\n{section_block}"
            else:
                updated = f"{existing.rstrip()}\n\n{section_block}"
            autopsy_file.write_text(updated, encoding="utf-8")

        return {
            "diff_path": target_diff_path,
            "baseline_path": target_baseline,
            "diff_result": diff_res,
        }

