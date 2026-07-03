#!/usr/bin/env python3
"""
Seed the deploy tracker with a realistic commit history for each service.
Run once after `docker compose up --build -d` before injecting any faults.

Usage: python3 scripts/seed_deploys.py [http://localhost:8090]
"""
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

BASE_URL = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:9000"


def post(path: str, data: dict) -> dict:
    body = json.dumps(data, default=str).encode()
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError as exc:
        print(f"  ERROR posting to {BASE_URL}{path}: {exc}")
        sys.exit(1)


def ago(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


COMMITS = [
    # ── payments-service: three innocuous commits ──────────────────────────
    {
        "sha": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
        "service": "payments-service",
        "deployed_at": ago(55),
        "author": "alice@example.com",
        "commit_message": "refactor: extract amount validation to helper",
        "branch": "main",
        "diff": """\
diff --git a/services/payments-service/main.py b/services/payments-service/main.py
index 1a2b3c4..5d6e7f8 100644
--- a/services/payments-service/main.py
+++ b/services/payments-service/main.py
@@ -18,8 +18,13 @@ MOCK_PAYMENTS = [...]

+def _validate_amount(amount) -> float:
+    if amount is None or float(amount) <= 0:
+        raise ValueError(f"Invalid amount: {amount}")
+    return round(float(amount), 2)
+
 @app.post("/payments/process")
 def process_payment(payment: dict):
     result = {
         "id": len(MOCK_PAYMENTS) + 1,
         "order_id": payment.get("order_id"),
-        "amount": payment.get("amount", 0),
+        "amount": _validate_amount(payment.get("amount")),
         "status": "completed",
     }
""",
    },
    {
        "sha": "b2c3d4e5f6b2c3d4e5f6b2c3d4e5f6b2c3d4e5f6",
        "service": "payments-service",
        "deployed_at": ago(35),
        "author": "charlie@example.com",
        "commit_message": "feat: add idempotency key to payment response",
        "branch": "main",
        "diff": """\
diff --git a/services/payments-service/main.py b/services/payments-service/main.py
index 5d6e7f8..7e8f9a0 100644
--- a/services/payments-service/main.py
+++ b/services/payments-service/main.py
@@ -28,6 +28,7 @@ def process_payment(payment: dict):
     result = {
         "id": len(MOCK_PAYMENTS) + 1,
         "order_id": payment.get("order_id"),
         "amount": _validate_amount(payment.get("amount")),
         "status": "completed",
+        "idempotency_key": payment.get("idempotency_key", str(uuid.uuid4())),
         "transaction_id": f"txn_{random.randint(100000, 999999)}",
     }
""",
    },
    {
        "sha": "c3d4e5f6c3d4e5f6c3d4e5f6c3d4e5f6c3d4e5f6",
        "service": "payments-service",
        "deployed_at": ago(15),
        "author": "alice@example.com",
        "commit_message": "chore: bump prometheus-client to 0.20.0",
        "branch": "main",
        "diff": """\
diff --git a/services/payments-service/requirements.txt b/services/payments-service/requirements.txt
index abc1234..def5678 100644
--- a/services/payments-service/requirements.txt
+++ b/services/payments-service/requirements.txt
@@ -1,4 +1,4 @@
 fastapi==0.104.1
 uvicorn[standard]==0.24.0
-prometheus-client==0.19.0
+prometheus-client==0.20.0
""",
    },

    # ── order-service: two innocuous commits ───────────────────────────────
    {
        "sha": "e5f6a7b8e5f6a7b8e5f6a7b8e5f6a7b8e5f6a7b8",
        "service": "order-service",
        "deployed_at": ago(50),
        "author": "dana@example.com",
        "commit_message": "feat: add GET /orders/{id}/status shorthand endpoint",
        "branch": "main",
        "diff": """\
diff --git a/services/order-service/main.py b/services/order-service/main.py
index 3f4e5d6..7a8b9c0 100644
--- a/services/order-service/main.py
+++ b/services/order-service/main.py
@@ -52,0 +53,6 @@ async def get_order(order_id: int):
+
+@app.get("/orders/{order_id}/status")
+async def get_order_status(order_id: int):
+    order = next((o for o in MOCK_ORDERS if o["id"] == order_id), None)
+    if not order:
+        raise HTTPException(status_code=404, detail="Order not found")
+    return {"id": order_id, "status": order.get("status", "unknown")}
""",
    },
    {
        "sha": "f6a7b8c9f6a7b8c9f6a7b8c9f6a7b8c9f6a7b8c9",
        "service": "order-service",
        "deployed_at": ago(20),
        "author": "dana@example.com",
        "commit_message": "fix: correct default status for new orders",
        "branch": "main",
        "diff": """\
diff --git a/services/order-service/main.py b/services/order-service/main.py
index 7a8b9c0..9c0d1e2 100644
--- a/services/order-service/main.py
+++ b/services/order-service/main.py
@@ -60,7 +60,7 @@ async def create_order(order: dict):
-    new_order = {"id": len(MOCK_ORDERS) + 1, **order, "status": "created"}
+    new_order = {"id": len(MOCK_ORDERS) + 1, **order, "status": "pending"}
     MOCK_ORDERS.append(new_order)
     return new_order
""",
    },
]


def main():
    print(f"Seeding deploy history → {BASE_URL}")
    print()
    for commit in COMMITS:
        result = post("/deploys", commit)
        sha_short = commit["sha"][:8]
        print(f"  ✓  {sha_short}  [{commit['service']}]  {commit['commit_message']}")
    print()
    print(f"Seeded {len(COMMITS)} commits. Ready to inject faults.")


if __name__ == "__main__":
    main()
