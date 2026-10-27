"""
Genesis Blackbox: Dogfooding Realistic Microservice Crash Simulation
Demonstrates end-to-end ASGI/SQL tracing, deep frame capture, and Causal Autopsy generation.
"""

import os
import sys

# Ensure parent directory is in sys.path
pkg_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if pkg_root not in sys.path:
    sys.path.insert(0, pkg_root)

from blackbox.recorder.wal_ring import WalRingBuffer
from blackbox.recorder.exception_hook import ExceptionHook
from blackbox.recorder.http_interceptor import HttpTraceContext
from blackbox.recorder.sql_interceptor import SqlTraceContext
from blackbox.ast_engine.graph import CodebaseGraph
from blackbox.compiler.autopsy import CausalCrashCompiler


class InsufficientFundsError(Exception):
    """Custom domain exception for credit balance depletion."""
    pass


def deduct_credits(user_id: int, required_credits: int, balance: int, auth_token: str):
    """Deepest frame where failure manifests."""
    if balance < required_credits:
        raise InsufficientFundsError(
            f"Account {user_id} requires {required_credits} credits but only has {balance} credits."
        )
    return balance - required_credits


def validate_inventory(user_id: int, item_id: str, quantity: int, balance: int):
    """Intermediate nested frame."""
    unit_cost = 150
    total_cost = unit_cost * quantity
    auth_header = "bearer_secret_session_token_992"
    return deduct_credits(
        user_id=user_id,
        required_credits=total_cost,
        balance=balance,
        auth_token=auth_header
    )


def handle_order(order_payload: dict):
    """Top-level request handler simulating microservice entrypoint."""
    uid = order_payload.get("user_id", 0)
    item = order_payload.get("item_id", "ITEM-001")
    qty = order_payload.get("quantity", 1)
    current_balance = 250
    return validate_inventory(user_id=uid, item_id=item, quantity=qty, balance=current_balance)


def run_dogfood_simulation(db_path: str = ".blackbox/dogfood_wal.sqlite", output_file: str = ".blackbox.md") -> str:
    print(f"[Dogfood] Initializing Blackbox WAL at {db_path}...")
    os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
    ring = WalRingBuffer(db_path=db_path)
    hook = ExceptionHook(wal_ring=ring)
    graph = CodebaseGraph()
    graph.parse_file(__file__)

    # 1. Simulate prior HTTP request
    with HttpTraceContext(
        wal_ring=ring,
        method="POST",
        url="/api/v1/orders/checkout",
        source="asgi_service"
    ) as http_trace:
        http_trace.set_request(
            headers={"authorization": "Bearer super_secret_user_jwt", "x-client-id": "web-v2"},
            body={"user_id": 992, "item_id": "GPU-CLUSTER-H100", "quantity": 10}
        )
        http_trace.set_response(status_code=500)
        print("[Dogfood] Recorded incoming HTTP checkout transaction.")

    # 2. Simulate prior SQL query
    with SqlTraceContext(
        wal_ring=ring,
        query="SELECT credit_balance, tier FROM user_accounts WHERE user_id = ?",
        params=[992]
    ):
        print("[Dogfood] Recorded SQL query: SELECT credit_balance ...")

    # 3. Execute nested call tree and catch exception
    captured_payload = None
    try:
        print("[Dogfood] Executing handle_order() with insufficient balance...")
        payload = {"user_id": 992, "item_id": "GPU-CLUSTER-H100", "quantity": 10}
        handle_order(payload)
    except InsufficientFundsError:
        exc_type, exc_val, tb = sys.exc_info()
        captured_payload = hook.handle_exception(exc_type, exc_val, tb)
        print(f"[Dogfood] Deep exception captured and written to WAL: {exc_val}")

    # 4. Compile Autopsy Capsule
    compiler = CausalCrashCompiler(wal_ring=ring, ast_graph=graph)
    autopsy_markdown = compiler.compile(captured_payload)

    # 5. Write to output file
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(autopsy_markdown)
    print(f"[Dogfood] Autopsy capsule successfully emitted to {output_file} ({len(autopsy_markdown)} bytes).")

    # Verify integrity
    assert "InsufficientFundsError" in autopsy_markdown, "Missing exception type in autopsy"
    assert "/api/v1/orders/checkout" in autopsy_markdown, "Missing HTTP transaction in autopsy"
    assert "user_accounts" in autopsy_markdown, "Missing SQL query in autopsy"
    assert "deduct_credits" in autopsy_markdown, "Missing culprit function in autopsy"
    print("[Dogfood] All integrity checks PASSED successfully.")

    return autopsy_markdown


if __name__ == "__main__":
    run_dogfood_simulation()
