"""
Showcase 1: Python + SQLite Order & Checkout Pipeline
Demonstrates runtime state capture across healthy baseline and failing runs.
"""

import os
import sys
import sqlite3
from typing import Optional

from blackbox.recorder.wal_ring import WalRingBuffer
from blackbox.recorder.sql_interceptor import SqlInterceptor
from blackbox.recorder.http_interceptor import HttpTraceContext


def run_pipeline(mode: str = "baseline", db_path: str = ".blackbox/wal.sqlite") -> None:
    """Executes order batch processing with SQL and HTTP tracing."""
    ring = WalRingBuffer(db_path=db_path)
    try:
        # Step 1: Outbound HTTP Payment Gateway Authorization
        with HttpTraceContext(ring, "POST", "https://api.payments.internal/v1/checkout/auth") as ctx:
            ctx.set_request(
                headers={
                    "Authorization": "Bearer sec_prod_token_888",
                    "Content-Type": "application/json"
                },
                body={"merchant_id": "m_1001", "batch_size": 2}
            )
            ctx.set_response(
                status_code=200,
                body={"authorized": True, "token": "tok_valid_99"}
            )

        # Step 2: Database Initialization & Schema Definition
        interceptor = SqlInterceptor(ring)
        raw_conn = sqlite3.connect(":memory:")
        conn = interceptor.hook_sqlite3_connection(raw_conn)

        conn.execute("""
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                item TEXT NOT NULL,
                total REAL NOT NULL,
                discount REAL NOT NULL CHECK(discount >= 0.0 AND discount <= 1.0)
            )
        """)

        # Step 3: Insert Orders
        # Order 1 (Always healthy)
        conn.execute(
            "INSERT INTO orders (id, user_id, item, total, discount) VALUES (?, ?, ?, ?, ?)",
            (1001, 42, "Cloud Enterprise Node", 120.0, 0.10)
        )

        # Order 2: In baseline mode, discount is valid (0.05).
        # In failing mode, simulates silent upstream shift where discount mutates to None.
        if mode == "baseline":
            user_id = 43
            item = "Fast Storage Block"
            total = 50.0
            discount: Optional[float] = 0.05
        else:
            user_id = 43
            item = "Fast Storage Block"
            total = 50.0
            discount = None  # Silent upstream mutation triggering NOT NULL constraint

        # This insertion succeeds in baseline, but triggers IntegrityError in failing run
        conn.execute(
            "INSERT INTO orders (id, user_id, item, total, discount) VALUES (?, ?, ?, ?, ?)",
            (1002, user_id, item, total, discount)
        )

    finally:
        ring.close()


if __name__ == "__main__":
    mode_arg = "baseline"
    wal_arg = ".blackbox/wal.sqlite"

    for idx, arg in enumerate(sys.argv):
        if arg == "--mode" and idx + 1 < len(sys.argv):
            mode_arg = sys.argv[idx + 1]
        elif arg == "--db" and idx + 1 < len(sys.argv):
            wal_arg = sys.argv[idx + 1]

    run_pipeline(mode=mode_arg, db_path=wal_arg)

