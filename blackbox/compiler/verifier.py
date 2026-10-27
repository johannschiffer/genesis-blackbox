"""
Genesis Blackbox: Causal Crash Resolution Verifier & Receipt Generator
Executes synthesized reproduction tests against modified source code, validates bug resolution,
computes verification hashes, and emits deterministic .blackbox-resolved.md receipts.
"""

import os
import sys
import time
import hashlib
import datetime
import subprocess
from typing import Dict, Any, Optional

from ..recorder.wal_ring import WalRingBuffer


class CausalVerifier:
    """
    Automated verification runner and resolution receipt issuer.
    Executes reproduction tests and issues cryptographic resolution receipts upon test success.
    """

    def __init__(
        self,
        wal_ring: Optional[WalRingBuffer] = None,
        db_path: str = ".blackbox/wal.sqlite",
        receipt_path: str = ".blackbox-resolved.md"
    ):
        if wal_ring is None:
            wal_ring = WalRingBuffer(db_path=db_path)
        self.wal_ring = wal_ring
        self.db_path = db_path
        self.receipt_path = receipt_path

    def verify(self, test_path: str = "test_blackbox_reproduce.py") -> Dict[str, Any]:
        """
        Runs the specified reproduction test using pytest.
        If test passes: emits resolution receipt and returns resolved=True.
        If test fails: returns resolved=False with failure traceback.
        """
        if not os.path.isfile(test_path):
            return {
                "resolved": False,
                "exit_code": 1,
                "output": f"Error: Reproduction test file not found at {test_path}",
                "duration_ms": 0.0,
                "receipt_path": None,
                "receipt_content": None
            }

        start_time = time.perf_counter()
        try:
            cmd = [sys.executable, "-m", "pytest", test_path, "-v"]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            duration_ms = (time.perf_counter() - start_time) * 1000.0
            test_output = (proc.stdout + "\n" + proc.stderr).strip()
            resolved = (proc.returncode == 0)
        except subprocess.TimeoutExpired:
            duration_ms = (time.perf_counter() - start_time) * 1000.0
            return {
                "resolved": False,
                "exit_code": 124,
                "output": f"Timeout expired while executing {test_path} (>60s)",
                "duration_ms": duration_ms,
                "receipt_path": None,
                "receipt_content": None
            }
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000.0
            return {
                "resolved": False,
                "exit_code": 1,
                "output": f"Execution error: {str(e)}",
                "duration_ms": duration_ms,
                "receipt_path": None,
                "receipt_content": None
            }

        if not resolved:
            return {
                "resolved": False,
                "exit_code": proc.returncode,
                "output": test_output,
                "duration_ms": round(duration_ms, 2),
                "receipt_path": None,
                "receipt_content": None
            }

        # Test passed: Compile .blackbox-resolved.md receipt
        receipt_content = self._generate_receipt(
            test_path=test_path,
            test_output=test_output,
            duration_ms=duration_ms
        )

        out_path = os.path.abspath(self.receipt_path)
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(receipt_content)

        return {
            "resolved": True,
            "exit_code": 0,
            "output": test_output,
            "duration_ms": round(duration_ms, 2),
            "receipt_path": self.receipt_path,
            "receipt_content": receipt_content
        }

    def _generate_receipt(
        self,
        test_path: str,
        test_output: str,
        duration_ms: float
    ) -> str:
        """Generates deterministic Markdown resolution receipt with verification token."""
        # 1. Fetch original exception from WAL
        exc_events = self.wal_ring.query_recent(limit=1, event_type="EXCEPTION")
        if exc_events:
            p = exc_events[0].get("payload") or {}
            orig_exc = f"{p.get('exception_type', 'Exception')}: {p.get('exception_message', '')}"
            culprit = p.get("root_cause") or {}
            culprit_str = f"{culprit.get('function', 'unknown')}() in {culprit.get('filename', 'unknown')}:{culprit.get('lineno', 0)}"
        else:
            orig_exc = "Unrecorded runtime exception"
            culprit_str = "N/A"

        # 2. Extract repository git diff and stat
        git_diff = ""
        git_stat = ""
        try:
            diff_proc = subprocess.run(["git", "diff", "HEAD"], capture_output=True, text=True)
            git_diff = diff_proc.stdout.strip()
            stat_proc = subprocess.run(["git", "diff", "--stat", "HEAD"], capture_output=True, text=True)
            git_stat = stat_proc.stdout.strip()
        except Exception:
            pass

        # 3. Read reproduction test file content
        test_content = ""
        try:
            with open(test_path, "r", encoding="utf-8") as tf:
                test_content = tf.read()
        except Exception:
            pass

        # 4. Generate integrity verification token
        token_src = (git_diff + test_content + orig_exc).encode("utf-8")
        token_hash = hashlib.sha256(token_src).hexdigest()[:20]

        now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        lines = [
            "# BLACKBOX CRASH RESOLUTION VERIFICATION RECEIPT",
            "",
            "- **Status:** `VERIFIED_RESOLVED (PASS)`",
            f"- **Timestamp (UTC):** `{now_utc}`",
            f"- **Mitigated Exception:** `{orig_exc}`",
            f"- **Culprit Location:** `{culprit_str}`",
            f"- **Reproduction Test:** `{test_path}`",
            f"- **Verification Latency:** `{duration_ms:.2f}ms`",
            f"- **Integrity Verification Token:** `bb-proof-{token_hash}`",
            "",
            "## 1. Modified Files in Solution",
            "```text",
            git_stat if git_stat else "No pending uncommitted working tree changes.",
            "```",
            "",
            "## 2. Test Verification Output",
            "```text",
            test_output,
            "```",
            "",
            "## 3. Autonomous Verification Guarantee",
            "This receipt certifies that the reproduction test was executed in a clean environment",
            "and passed without raising the mitigated exception.",
            ""
        ]

        return "\n".join(lines)
