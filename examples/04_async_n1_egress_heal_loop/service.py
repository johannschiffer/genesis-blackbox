"""Target async service with N+1 SQL, outbound HTTP call, and crash bug for Example 04."""

import json
import sqlite3
import urllib.request


async def enrich_and_bill_order(order_id: int, base_url: str, api_key: str, db_path: str, discount_rate=None):
    """Fetches line items (N+1 pattern), queries pricing API, and computes total."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT id FROM line_items WHERE order_id = ?", (order_id,)).fetchall()
        subtotal = 0.0
        for (item_id,) in rows:
            # Deliberate N+1 SQL pattern inside loop
            row = conn.execute("SELECT price FROM item_catalog WHERE id = ?", (item_id,)).fetchone()
            if row:
                subtotal += float(row[0])
    finally:
        conn.close()

    with urllib.request.urlopen(f"{base_url}/pricing?api_key={api_key}", timeout=5) as resp:
        pricing_payload = json.loads(resp.read().decode("utf-8"))

    multiplier = float(pricing_payload.get("tax_multiplier", 1.0))
    # Deliberate crash when discount_rate is None
    return subtotal * multiplier - discount_rate
