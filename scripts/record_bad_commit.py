#!/usr/bin/env python3
"""
Record the simulated bad commit for a given service into the deploy tracker.
Called by inject_fault.sh immediately before the service is restarted.

Usage: python3 scripts/record_bad_commit.py <service> [base_url]
"""
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

SERVICE = sys.argv[1] if len(sys.argv) > 1 else "payments-service"
BASE_URL = (sys.argv[2].rstrip("/") if len(sys.argv) > 2 else "http://localhost:9000")

BAD_COMMITS = {
    "payments-service": {
        "sha": "d4e5f6a7d4e5f6a7d4e5f6a7d4e5f6a7d4e5f6a7",
        "service": "payments-service",
        "author": "bob@example.com",
        "commit_message": "feat: integrate ExternalPaymentGateway for lower processing fees",
        "branch": "feat/external-gateway",
        "is_fault": True,
        "diff": """\
diff --git a/services/payments-service/main.py b/services/payments-service/main.py
index 7e8f9a0..bad1234 100644
--- a/services/payments-service/main.py
+++ b/services/payments-service/main.py
@@ -1,6 +1,7 @@
 import os
 import random
 import time
+from external_gateway import PaymentGateway, GatewayTimeoutError
 from fastapi import FastAPI, Response, HTTPException, Request
 from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

@@ -44,10 +45,17 @@ def list_payments():
     return MOCK_PAYMENTS

 @app.post("/payments/process")
 def process_payment(payment: dict):
-    if FAULT_MODE == "exception":
-        if random.random() < 0.8:
-            raise HTTPException(status_code=500, ...)
+    # Route through ExternalPaymentGateway (lower fees, async settlement)
+    gw = PaymentGateway(api_key=os.getenv("GATEWAY_API_KEY"), timeout=0.5)
+    try:
+        charge = gw.charge(
+            amount=payment.get("amount"),
+            token=payment.get("payment_token"),  # None for guest checkouts
+        )
+    except GatewayTimeoutError as exc:
+        # Re-raise as 500 — TODO: add retry logic before merging
+        raise HTTPException(status_code=500, detail=str(exc))
+
     result = {
         "id": len(MOCK_PAYMENTS) + 1,
         "order_id": payment.get("order_id"),
-        "amount": payment.get("amount", 0),
+        "amount": charge.amount,
         "status": "completed",
-        "transaction_id": f"txn_{random.randint(100000, 999999)}",
+        "transaction_id": charge.transaction_id,
     }
""",
    },
    "order-service": {
        "sha": "b8c9d0e1b8c9d0e1b8c9d0e1b8c9d0e1b8c9d0e1",
        "service": "order-service",
        "author": "eve@example.com",
        "commit_message": "perf: enrich order listing with per-order customer and line-item details",
        "branch": "feat/enriched-orders",
        "is_fault": True,
        "diff": """\
diff --git a/services/order-service/main.py b/services/order-service/main.py
index 9c0d1e2..bad5678 100644
--- a/services/order-service/main.py
+++ b/services/order-service/main.py
@@ -34,8 +34,26 @@ app = FastAPI(title="Order Service")

 @app.get("/orders")
 async def list_orders():
-    if FAULT_MODE == "n_plus_one":
-        for _ in MOCK_ORDERS:
-            await asyncio.sleep(0.1)
-    return MOCK_ORDERS
+    # Enrich each order with live customer profile + line items
+    # Previously returned bare order objects; product requested full details
+    enriched = []
+    for order in MOCK_ORDERS:
+        customer = await _fetch_customer(order["customer_id"])
+        line_items = await _fetch_line_items(order["id"])
+        enriched.append({
+            **order,
+            "customer": customer,
+            "line_items": line_items,
+        })
+    return enriched
+
+
+async def _fetch_customer(customer_id: str) -> dict:
+    # Hits customer-service per order — was previously batched via JOIN
+    await asyncio.sleep(0.08)
+    return {"id": customer_id, "name": f"Customer {customer_id}", "tier": "standard"}
+
+
+async def _fetch_line_items(order_id: int) -> list:
+    # Separate DB round-trip per order
+    await asyncio.sleep(0.09)
+    return [{"sku": f"SKU-{order_id}-{i}", "qty": 1} for i in range(3)]
""",
    },
}

if SERVICE not in BAD_COMMITS:
    print(f"No bad commit defined for service: {SERVICE}")
    sys.exit(0)

commit = BAD_COMMITS[SERVICE]
commit["deployed_at"] = datetime.now(timezone.utc).isoformat()

body = json.dumps(commit, default=str).encode()
req = urllib.request.Request(
    f"{BASE_URL}/deploys",
    data=body,
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())
    print(f"  Bad commit recorded: {commit['sha'][:8]} ({SERVICE})")
except urllib.error.URLError as exc:
    print(f"  WARNING: could not record bad commit to {BASE_URL}: {exc}")
    print("  (Start agent-backend first, or run seed_deploys.py manually)")
