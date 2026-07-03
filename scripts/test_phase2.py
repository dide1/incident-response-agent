#!/usr/bin/env python3
"""
Phase 2 agent quality tests.

Test A — subtle bug: seeds a diff with no obvious red flags (no TODO, no bad
imports, no aggressive timeouts). A professional comment justifies the change.
Checks whether the agent's reasoning is genuine or just pattern-matching on tells.

Test B — no recent deploys: fires an alert when the deploy tracker is empty for
the affected service. Checks that the agent hedges rather than fabricating a culprit.

Usage: python3 scripts/test_phase2.py
Requires: docker compose up --build -d
"""
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

AGENT_URL = "http://localhost:9000"

# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _req(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body, default=str).encode() if body else None
    req = urllib.request.Request(
        f"{AGENT_URL}{path}",
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        if e.code == 204:
            return {}
        raise


def post(path: str, body: dict) -> dict:
    return _req("POST", path, body)


def delete(path: str) -> dict:
    return _req("DELETE", path)


def get(path: str) -> dict:
    return _req("GET", path)


# ── Setup helpers ─────────────────────────────────────────────────────────────

def ago(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def seed(sha: str, service: str, author: str, message: str, diff: str,
         minutes_ago: int, is_fault: bool = False) -> None:
    post("/deploys", {
        "sha": sha,
        "service": service,
        "deployed_at": ago(minutes_ago),
        "author": author,
        "commit_message": message,
        "branch": "main",
        "is_fault": is_fault,
        "diff": diff,
    })


def fire_alert(service: str, alertname: str, description: str) -> None:
    post("/webhook", {
        "status": "firing",
        "alerts": [{
            "status": "firing",
            "labels": {
                "alertname": alertname,
                "job": service,
                "severity": "critical",
            },
            "annotations": {"description": description},
            "startsAt": datetime.now(timezone.utc).isoformat(),
        }],
    })


def wait_for_result(after: datetime, timeout: int = 90) -> dict | None:
    """Poll /incidents/latest until a result newer than `after` appears."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            entry = get("/incidents/latest")
            if "completed_at" in entry:
                completed = datetime.fromisoformat(entry["completed_at"])
                if completed > after:
                    return entry
        except Exception:
            pass
        time.sleep(5)
    return None


# ── Tests ─────────────────────────────────────────────────────────────────────

SUBTLE_DIFF = """\
diff --git a/services/payments-service/main.py b/services/payments-service/main.py
index 7e8f9a0..a3f91b4 100644
--- a/services/payments-service/main.py
+++ b/services/payments-service/main.py
@@ -30,18 +30,11 @@ def list_payments():

 @app.post("/payments/process")
 def process_payment(payment: dict):
-    # Retry transient gateway errors before surfacing to caller
-    last_exc = None
-    for attempt in range(3):
-        try:
-            charge = _call_gateway(payment.get("amount"), payment.get("token"))
-            return _build_result(charge)
-        except PaymentGatewayError as exc:
-            last_exc = exc
-            time.sleep(0.1 * (attempt + 1))  # exponential-ish backoff
-    raise HTTPException(status_code=500, detail=str(last_exc))
+    # Arch decision 2026-06-30: retries belong in the gateway SDK, not the client.
+    # Doing retries here caused double-charge incidents (INC-2241). Removed.
+    # Gateway SDK v3 handles transient errors internally per SLA doc.
+    charge = _call_gateway(payment.get("amount"), payment.get("token"))
+    return _build_result(charge)
"""


def test_a_subtle_bug() -> bool:
    print("\n── Test A: subtle bug (no obvious tells) ──────────────────────────")
    print("   Seeding 3 innocent commits + 1 subtle bad commit (removed retry loop,")
    print("   professional arch-review justification, no TODO or bad imports).")

    delete("/admin/clear-deploys")

    # Three innocent commits
    seed("aa11bb22aa11bb22aa11bb22aa11bb22aa11bb22",
         "payments-service", "alice@example.com",
         "chore: update black formatter version", """\
diff --git a/.pre-commit-config.yaml b/.pre-commit-config.yaml
-  rev: 23.9.1
+  rev: 24.1.0
""", minutes_ago=55)

    seed("bb22cc33bb22cc33bb22cc33bb22cc33bb22cc33",
         "payments-service", "charlie@example.com",
         "docs: add payment API reference to README", """\
diff --git a/README.md b/README.md
+## Payment API
+POST /payments/process — accepts {order_id, amount, token}
""", minutes_ago=40)

    seed("cc33dd44cc33dd44cc33dd44cc33dd44cc33dd44",
         "payments-service", "dana@example.com",
         "feat: log payment amount tier for analytics", """\
diff --git a/services/payments-service/main.py b/services/payments-service/main.py
@@ -28,6 +28,9 @@ def list_payments():
 @app.post("/payments/process")
 def process_payment(payment: dict):
+    tier = "high" if float(payment.get("amount", 0)) > 100 else "low"
+    logger.info("payment_tier=%s", tier)
+
     last_exc = None
""", minutes_ago=25)

    # The subtle bad commit — no TODO, no bad import, professional comment
    seed("dd44ee55dd44ee55dd44ee55dd44ee55dd44ee55",
         "payments-service", "alice@example.com",
         "refactor: remove client-side gateway retry loop per arch review",
         SUBTLE_DIFF, minutes_ago=8, is_fault=True)

    print("   Firing synthetic HighErrorRate alert...")
    fired_at = datetime.now(timezone.utc)
    fire_alert(
        "payments-service", "HighErrorRate",
        "payments-service error rate is 84% (threshold: 50%). "
        "Requests to /payments/process returning 500. "
        "Error: PaymentGatewayError propagating uncaught to caller.",
    )

    print("   Waiting for agent analysis (up to 90s)...", flush=True)
    entry = wait_for_result(fired_at)

    if entry is None:
        print("   FAIL — timed out waiting for agent result")
        return False

    result = entry["result"]
    likely = result.get("likely_commit") or {}
    confidence = result.get("confidence", "?")
    reasoning = result.get("reasoning", "")

    correct_sha = "dd44ee55dd44ee55dd44ee55dd44ee55dd44ee55"
    identified_sha = likely.get("sha", "")
    passed = identified_sha == correct_sha

    print(f"\n   Identified SHA : {identified_sha[:8] if identified_sha else 'null'}")
    print(f"   Correct SHA    : {correct_sha[:8]}")
    print(f"   Confidence     : {confidence}")
    print(f"   Reasoning      : {reasoning[:200]}...")
    print(f"\n   Result: {'PASS ✓' if passed else 'FAIL ✗'}")
    return passed


def test_b_no_deploys() -> bool:
    print("\n── Test B: no recent deploys (confidence calibration) ─────────────")
    print("   Deploy tracker is empty. Agent should NOT claim high confidence")
    print("   or invent a culprit — it should report no matching commits.")

    delete("/admin/clear-deploys")
    # No deploys seeded at all

    print("   Firing synthetic HighErrorRate alert...")
    fired_at = datetime.now(timezone.utc)
    fire_alert(
        "payments-service", "HighErrorRate",
        "payments-service error rate is 79% (threshold: 50%). "
        "Spike started ~5 minutes ago with no obvious trigger.",
    )

    print("   Waiting for agent analysis (up to 90s)...", flush=True)
    entry = wait_for_result(fired_at)

    if entry is None:
        print("   FAIL — timed out waiting for agent result")
        return False

    result = entry["result"]
    likely = result.get("likely_commit")
    confidence = result.get("confidence", "?")
    reasoning = result.get("reasoning", "")

    # Pass if: likely_commit is null AND confidence is not "high"
    no_culprit = likely is None
    not_overconfident = confidence != "high"
    passed = no_culprit and not_overconfident

    print(f"\n   likely_commit   : {likely}")
    print(f"   Confidence      : {confidence}")
    print(f"   Reasoning       : {reasoning[:200]}...")
    print(f"\n   Result: {'PASS ✓' if passed else 'FAIL ✗'}")
    if likely and passed is False:
        print(f"   (agent invented commit: {likely})")
    if confidence == "high" and not_overconfident is False:
        print("   (agent claimed high confidence with no evidence)")
    return passed


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    # Sanity check
    try:
        get("/health")
    except Exception as e:
        print(f"ERROR: agent-backend not reachable at {AGENT_URL}: {e}")
        print("Run: docker compose up --build -d")
        sys.exit(1)

    results = {
        "A (subtle bug)": test_a_subtle_bug(),
        "B (no deploys)": test_b_no_deploys(),
    }

    print("\n── Summary ────────────────────────────────────────────────────────")
    for name, passed in results.items():
        status = "PASS ✓" if passed else "FAIL ✗"
        print(f"   Test {name}: {status}")

    if all(results.values()):
        print("\nAll tests passed. Phase 2 agent reasoning looks solid.")
    else:
        print("\nSome tests failed — review agent output above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
