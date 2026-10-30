"""
Genesis Blackbox: Portable Crash Bundler
Packages self-contained, shareable incident archives (.zip) with WAL snapshots,
causal autopsies, execution diffs, reproduction scripts, and SHA-256 manifests.
"""

import os
import sys
import json
import zipfile
import hashlib
import sqlite3
import platform
import tempfile
import subprocess
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple

from .wal_ring import WalRingBuffer
from ..ast_engine.graph import CodebaseGraph
from ..compiler.autopsy import CausalCrashCompiler
from ..compiler.reproducer import CrashReproducer
from ..compiler.state_differ import StateDiffer


class CrashBundler:
    """
    Creates and inspects portable, tamper-evident incident bundles (.zip)
    containing an online SQLite backup of wal.sqlite, autopsy capsule,
    execution diffs, reproduction tests, and a SHA-256 checksum manifest.
    """

    def __init__(self, db_path: str = ".blackbox/wal.sqlite", repo_root: str = "."):
        self.db_path = db_path
        self.repo_root = os.path.abspath(repo_root)

    def create_bundle(
        self,
        output_zip: str = "crash_bundle.zip",
        db_path: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Snapshots active SQLite WAL database safely via Online Backup API
        and packages autopsy, diff, reproducer test, and system_manifest.json into a .zip.
        """
        source_db = db_path or self.db_path
        if not os.path.exists(source_db):
            # Ensure parent dir exists and initialize empty wal if missing
            os.makedirs(os.path.dirname(os.path.abspath(source_db)) or ".", exist_ok=True)
            empty_ring = WalRingBuffer(db_path=source_db)
            empty_ring.close()

        output_zip_path = os.path.abspath(output_zip)
        os.makedirs(os.path.dirname(output_zip_path) or ".", exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="blackbox_bundle_") as temp_dir:
            temp_db_path = os.path.join(temp_dir, "wal.sqlite")

            # 1. Safe SQLite Online Backup API snapshot
            src_conn = sqlite3.connect(source_db)
            dest_conn = sqlite3.connect(temp_db_path)
            with dest_conn:
                src_conn.backup(dest_conn)
            dest_conn.close()
            src_conn.close()

            # 2. Extract WAL event statistics from snapshot
            total_events = 0
            event_type_counts: Dict[str, int] = {}
            with sqlite3.connect(temp_db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT count(*) FROM blackbox_events")
                total_events = cur.fetchone()[0]
                cur.execute("SELECT event_type, count(*) FROM blackbox_events GROUP BY event_type")
                for et, count in cur.fetchall():
                    event_type_counts[et] = count

            temp_ring = WalRingBuffer(db_path=temp_db_path)
            ast_graph = CodebaseGraph()

            # 3. Synthesize Autopsy Capsule (.blackbox.md)
            compiler = CausalCrashCompiler(wal_ring=temp_ring, ast_graph=ast_graph)
            autopsy_md = compiler.compile()

            # 4. Synthesize Standalone Reproduction Test
            reproducer = CrashReproducer(wal_ring=temp_ring)
            repro_code = reproducer.generate()

            recent_excs = temp_ring.query_recent(limit=1, event_type="EXCEPTION")
            is_node = False
            if recent_excs:
                p = recent_excs[0].get("payload", {})
                if p.get("runtime") == "node" or "node:test" in repro_code:
                    is_node = True

            repro_filename = "test_blackbox_reproduce.mjs" if is_node else "test_blackbox_reproduce.py"

            # 5. Synthesize Time-Travel Execution Diff (if >= 2 runs)
            diff_md: Optional[str] = None
            try:
                differ = StateDiffer(wal_ring=temp_ring)
                diff_res = differ.diff()
                if diff_res and diff_res.get("divergence_point"):
                    diff_md = differ.compile_markdown(diff_res)
            except Exception:
                diff_md = None

            temp_ring.close()

            # 6. Gather System & Git Manifest Metadata
            git_branch = None
            git_commit = None
            try:
                rb = subprocess.run(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    cwd=self.repo_root,
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if rb.returncode == 0:
                    git_branch = rb.stdout.strip()
                rc = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=self.repo_root,
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if rc.returncode == 0:
                    git_commit = rc.stdout.strip()
            except Exception:
                pass

            node_version = None
            try:
                rn = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=5)
                if rn.returncode == 0:
                    node_version = rn.stdout.strip()
            except Exception:
                pass

            # 7. Collect files and calculate SHA-256 hashes
            bundle_files: Dict[str, bytes] = {}
            with open(temp_db_path, "rb") as f_db:
                bundle_files["wal.sqlite"] = f_db.read()

            bundle_files[".blackbox.md"] = autopsy_md.encode("utf-8")
            bundle_files[repro_filename] = repro_code.encode("utf-8")

            if diff_md:
                bundle_files[".blackbox-diff.md"] = diff_md.encode("utf-8")

            checksums: Dict[str, str] = {}
            for fname, content_bytes in bundle_files.items():
                checksums[fname] = hashlib.sha256(content_bytes).hexdigest()

            # 8. Build system_manifest.json
            manifest: Dict[str, Any] = {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "os": {
                    "system": platform.system(),
                    "release": platform.release(),
                    "machine": platform.machine(),
                    "platform": platform.platform()
                },
                "python_version": sys.version,
                "node_version": node_version,
                "git": {
                    "branch": git_branch,
                    "commit": git_commit
                },
                "wal_stats": {
                    "total_events": total_events,
                    "event_types": event_type_counts
                },
                "files": sorted(list(bundle_files.keys())),
                "checksums": checksums
            }

            manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")
            bundle_files["system_manifest.json"] = manifest_bytes

            # 9. Write ZIP archive
            with zipfile.ZipFile(output_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for fname, data in bundle_files.items():
                    zf.writestr(fname, data)

        size_bytes = os.path.getsize(output_zip_path)
        return {
            "success": True,
            "bundle_path": output_zip_path,
            "size_bytes": size_bytes,
            "files": sorted(list(bundle_files.keys())),
            "manifest": manifest
        }

    def inspect_bundle(self, zip_path: str) -> Dict[str, Any]:
        """
        Inspects a crash bundle archive and verifies SHA-256 member integrity
        without extracting files to disk.
        """
        target_path = os.path.abspath(zip_path)
        if not os.path.isfile(target_path):
            return {
                "valid": False,
                "error": f"Bundle file not found: {zip_path}",
                "manifest": None,
                "corrupted_files": [],
                "files": []
            }

        try:
            with zipfile.ZipFile(target_path, "r") as zf:
                namelist = zf.namelist()
                if "system_manifest.json" not in namelist:
                    return {
                        "valid": False,
                        "error": "Archive missing system_manifest.json",
                        "manifest": None,
                        "corrupted_files": [],
                        "files": namelist
                    }

                try:
                    manifest_raw = zf.read("system_manifest.json").decode("utf-8")
                    manifest = json.loads(manifest_raw)
                except Exception as e:
                    return {
                        "valid": False,
                        "error": f"Failed to parse system_manifest.json: {e}",
                        "manifest": None,
                        "corrupted_files": [],
                        "files": namelist
                    }

                checksums = manifest.get("checksums", {})
                corrupted: List[Dict[str, str]] = []

                for member_name, expected_sha in checksums.items():
                    if member_name not in namelist:
                        corrupted.append({
                            "file": member_name,
                            "error": "File listed in manifest is missing from archive"
                        })
                        continue

                    data = zf.read(member_name)
                    actual_sha = hashlib.sha256(data).hexdigest()
                    if actual_sha != expected_sha:
                        corrupted.append({
                            "file": member_name,
                            "error": "SHA-256 checksum mismatch (tampered or corrupted)",
                            "expected": expected_sha,
                            "actual": actual_sha
                        })

                is_valid = len(corrupted) == 0
                return {
                    "valid": is_valid,
                    "manifest": manifest,
                    "corrupted_files": corrupted,
                    "files": namelist
                }
        except zipfile.BadZipFile:
            return {
                "valid": False,
                "error": "File is not a valid zip archive",
                "manifest": None,
                "corrupted_files": [],
                "files": []
            }
        except Exception as e:
            return {
                "valid": False,
                "error": str(e),
                "manifest": None,
                "corrupted_files": [],
                "files": []
            }


def create_bundle(
    output_zip: str = "crash_bundle.zip",
    db_path: str = ".blackbox/wal.sqlite",
    repo_root: str = "."
) -> Dict[str, Any]:
    """Helper function to create a portable incident bundle."""
    bundler = CrashBundler(db_path=db_path, repo_root=repo_root)
    return bundler.create_bundle(output_zip=output_zip, db_path=db_path)


def inspect_bundle(zip_path: str) -> Dict[str, Any]:
    """Helper function to inspect and verify a crash bundle."""
    bundler = CrashBundler()
    return bundler.inspect_bundle(zip_path=zip_path)
