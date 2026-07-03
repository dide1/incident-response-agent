#!/usr/bin/env python3
"""
Phase 5 test: postmortem auto-generation on alert resolution.

Flow:
  1. Seed N+1 commit for order-service  (HighLatency scenario)
  2. Fire "firing" webhook → wait for agent analysis
  3. POST /admin/generate-postmortem (simulates resolved webhook)
  4. Wait for postmortem in /postmortems/latest
  5. Verify: required sections, commit SHA, timeline, action items
  6. Print the full postmortem so we can read it
"""
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

BASE = "http://localhost:9000"
BOLD = "\033[1m"
GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"
YELLOW = "\033[33m"


def _req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else b""
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data or None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        return exc.code, {}


def ok(msg):   print(f"  {GREEN}✓{RESET}  {msg}")
def warn(msg): print(f"  {YELLOW}⚠{RESET}  {msg}")
def fail(msg): print(f"  {RED}✗{RESET}  {msg}"); sys.exit(1)
def section(t): print(f"\n{BOLD}{t}{RESET}")


def wait_for_incident(fired_at: datetime, timeout: int = 120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, data = _req("GET", "/incidents/latest")
        if "completed_at" in data:
            completed = datetime.fromisoformat(data["completed_at"].replace("Z", "+00:00"))
            if completed > fired_at:
                return data
        time.sleep(3)
    fail(f"Agent did not complete within {timeout}s")


def wait_for_postmortem(after: datetime, timeout: int = 90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, data = _req("GET", "/postmortems/latest")
        if "created_at" in data:
            created = datetime.fromisoformat(data["created_at"].replace("Z", "+00:00"))
            if created > after:
                return data
        time.sleep(3)
    fail(f"Postmortem did not appear within {timeout}s")


# ── Setup: N+1 latency scenario ─────────────────────────────────────────────
section("Setup — seed N+1 bad commit for order-service")

_req("DELETE", "/admin/clear-deploys")

bad_sha = "aaaa" + "0" * 36
_req("POST", "/deploys", {
    "sha": bad_sha,
    "service": "order-service",
    "author": "alice@example.com",
    "commit_message": "refactor: iterate customers inline for display",
    "is_fault": True,
    "diff": (
        "- rows = db.fetch_all('SELECT * FROM customers WHERE id = ANY(%s)', all_ids)\n"
        "+ for order in orders:\n"
        "+     order['customer'] = db.fetch_one("
        "'SELECT * FROM customers WHERE id = %s', order['customer_id'])\n"
    ),
})
ok("N+1 bad commit seeded")


# ── Step 1: Fire HighLatency alert ───────────────────────────────────────────
section("Step 1 — Fire HighLatency webhook")

fired_at = datetime.now(timezone.utc)
_req("POST", "/webhook", {
    "version": "4",
    "status": "firing",
    "alerts": [{
        "status": "firing",
        "labels": {
            "alertname": "HighLatency",
            "job": "order-service",
            "severity": "warning",
        },
        "annotations": {
            "description": (
                "P99 latency on order-service is 3.8s. "
                "Requests to /orders are slow. Database query patterns may be involved."
            )
        },
        "startsAt": fired_at.isoformat(),
    }],
})
ok(f"Webhook fired at {fired_at.strftime('%H:%M:%S UTC')}")


# ── Step 2: Wait for agent analysis ──────────────────────────────────────────
section("Step 2 — Wait for agent analysis (up to 120s)")

entry = wait_for_incident(fired_at, timeout=120)
result = entry.get("result", {})
commit = result.get("likely_commit") or {}
ok(f"Agent complete — blamed {commit.get('sha','')[:8]} ({result.get('confidence')} confidence)")
ok(f"Runbook: {(result.get('runbook') or {}).get('filename', 'none')}")


# ── Step 3: Trigger postmortem generation ────────────────────────────────────
section("Step 3 — Trigger postmortem (simulate alert resolved)")

resolved_at = datetime.now(timezone.utc)
before_pm = datetime.now(timezone.utc)

# Option A: send a real resolved webhook
_req("POST", "/webhook", {
    "version": "4",
    "status": "resolved",
    "alerts": [{
        "status": "resolved",
        "labels": {
            "alertname": "HighLatency",
            "job": "order-service",
            "severity": "warning",
        },
        "annotations": {},
        "startsAt": fired_at.isoformat(),
        "endsAt": resolved_at.isoformat(),
    }],
})
ok(f"Resolved webhook sent at {resolved_at.strftime('%H:%M:%S UTC')}")


# ── Step 4: Wait for postmortem ──────────────────────────────────────────────
section("Step 4 — Wait for postmortem (up to 90s)")

pm_row = wait_for_postmortem(before_pm, timeout=90)
content = pm_row.get("content", "")
ok(f"Postmortem generated ({len(content)} chars)")


# ── Step 5: Validate postmortem content ──────────────────────────────────────
section("Step 5 — Validate postmortem structure")

REQUIRED_SECTIONS = [
    "## Executive Summary",
    "## Timeline",
    "## Root Cause",
    "## Impact",
    "## Detection & Response",
    "## Action Items",
    "## Contributing Factors",
    "## Lessons Learned",
]

for heading in REQUIRED_SECTIONS:
    if heading in content:
        ok(f"Section present: {heading}")
    else:
        fail(f"Section missing: {heading}")

# Timeline table
if "| Time" in content or "|---" in content:
    ok("Timeline Markdown table present")
else:
    warn("Timeline table not found — may be formatted differently")

# Commit SHA
sha_short = bad_sha[:8]
if sha_short in content:
    ok(f"Commit SHA {sha_short} referenced in postmortem")
else:
    warn(f"Commit SHA {sha_short} not found in postmortem text")

# Action items (checkboxes)
checkbox_count = content.count("- [ ]")
if checkbox_count >= 2:
    ok(f"{checkbox_count} action item checkboxes")
else:
    fail(f"Expected ≥2 action items (- [ ] ...), found {checkbox_count}")

# N+1 keyword
if "n+1" in content.lower() or "n plus" in content.lower() or "per-item" in content.lower() or "any(" in content.lower():
    ok("Root cause (N+1 pattern) mentioned in postmortem")
else:
    warn("N+1 pattern not explicitly named — check root cause section")


# ── Step 6: Print the postmortem ─────────────────────────────────────────────
section("Step 6 — Full postmortem")
print()
for line in content.splitlines():
    print(f"  {line}")


print(f"\n{BOLD}{GREEN}Phase 5 PASSED{RESET} — postmortem auto-generated on alert resolution\n")
