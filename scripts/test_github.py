#!/usr/bin/env python3
"""
GitHub integration test: real repo data replacing the seeded answer key.

Tests:
  A. github_client lists real commits from the configured repo (cross-checked
     against local `git log` — the shas must be real, not fabricated)
  B. github_client fetches a real diff for HEAD
  C. get_ci_logs degrades gracefully when the repo has no Actions runs
  D. End-to-end: synthetic CIFailure webhook → agent investigates REAL commits
     via the GitHub API and produces a structured analysis
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

REPO_FULL = "dide1/incident-response-agent"
SERVICE = REPO_FULL.split("/")[-1]
REPO_DIR = os.path.join(os.path.dirname(__file__), "..")

BASE = "http://localhost:9000"
BOLD = "\033[1m"
GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"
YELLOW = "\033[33m"


def ok(msg):   print(f"  {GREEN}✓{RESET}  {msg}")
def warn(msg): print(f"  {YELLOW}⚠{RESET}  {msg}")
def fail(msg): print(f"  {RED}✗{RESET}  {msg}"); sys.exit(1)
def section(t): print(f"\n{BOLD}{t}{RESET}")


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


# Import the client directly (host-side) with the repo configured
os.environ["GITHUB_REPOS"] = REPO_FULL
sys.path.insert(0, os.path.join(REPO_DIR, "agent-backend"))
from github_client import fetch_ci_logs, fetch_commit_diff, list_recent_commits  # noqa: E402


# ── Test A: real commits, cross-checked against local git log ───────────────
section("Test A — Real commit listing (cross-checked with local git log)")

rows = list_recent_commits(SERVICE, window_minutes=90)
if not rows or "error" in rows[0]:
    fail(f"list_recent_commits returned: {rows}")
ok(f"{len(rows)} commits returned from GitHub API")

local_shas = subprocess.run(
    ["git", "log", "--format=%H", "-30"],
    cwd=REPO_DIR, capture_output=True, text=True,
).stdout.split()

api_shas = [r["sha"] for r in rows]
matched = [s for s in api_shas if s in local_shas]
if len(matched) != len(api_shas):
    unmatched = set(api_shas) - set(matched)
    fail(f"API returned shas not in local git log (fabricated?): {unmatched}")
ok(f"All {len(api_shas)} shas verified against local git log — data is real")

for r in rows[:3]:
    print(f"      {r['sha'][:8]}  {r['author']}  {r['commit_message'][:60]}")


# ── Test B: real diff ────────────────────────────────────────────────────────
section("Test B — Real diff fetch for HEAD")

head_sha = api_shas[0]
diff_row = fetch_commit_diff(head_sha)
if not diff_row or not diff_row.get("diff"):
    fail(f"No diff returned for {head_sha[:8]}")
diff = diff_row["diff"]
if "diff --git" not in diff:
    fail(f"Diff doesn't look like a real git diff: {diff[:100]}")
ok(f"Real diff fetched for {head_sha[:8]} ({len(diff)} chars)")
first_file = next((l for l in diff.splitlines() if l.startswith("diff --git")), "")
print(f"      {first_file}")


# ── Test C: CI logs graceful degradation ────────────────────────────────────
section("Test C — get_ci_logs handles repo without Actions runs")

ci = fetch_ci_logs(SERVICE)
if "error" in ci:
    ok(f"Graceful error (expected — no Actions in this repo): {ci['error']}")
elif "run_id" in ci:
    ok(f"Found real failed run {ci['run_id']} ({ci.get('workflow')}) — even better")
else:
    fail(f"Unexpected shape: {ci}")


# ── Test D: end-to-end — agent investigates real commits ────────────────────
section("Test D — Agent investigates real GitHub commits (CIFailure alert)")

fired_at = datetime.now(timezone.utc)
_req("POST", "/webhook", {
    "version": "4",
    "status": "firing",
    "alerts": [{
        "status": "firing",
        "labels": {
            "alertname": "CIFailure",
            "job": SERVICE,
            "severity": "warning",
        },
        "annotations": {
            "description": (
                f"GitHub Actions workflow 'tests' failed on branch main at commit "
                f"{head_sha[:8]}. No run ID available (synthetic trigger for integration test)."
            )
        },
        "startsAt": fired_at.isoformat(),
    }],
})
print(f"  CIFailure webhook fired for {SERVICE} — agent should hit the real GitHub API")
print("  Waiting for agent (up to 120s) ...")

entry = wait_for_incident(fired_at, timeout=120)
result = entry.get("result", {})

tool_log = result.get("tool_log", [])
deploy_calls = [t for t in tool_log if t["tool"] == "get_recent_deploys"]
if not deploy_calls:
    fail("Agent never called get_recent_deploys")

returned_shas = [
    r.get("sha") for call in deploy_calls for r in call.get("result", [])
    if isinstance(r, dict) and r.get("sha")
]
real = [s for s in returned_shas if s in local_shas]
if not real:
    fail(f"Agent saw no real shas — got: {[s[:8] for s in returned_shas]}")
ok(f"Agent's deploy view contained {len(real)} real commits from GitHub")

leaked = any(
    "is_fault" in r
    for call in deploy_calls for r in call.get("result", [])
    if isinstance(r, dict)
)
if leaked:
    fail("is_fault leaked into agent context — answer-key fix regressed")
ok("No is_fault in agent context (answer-key leak fixed)")

if any(t["tool"] == "get_ci_logs" for t in tool_log):
    ok("Agent called get_ci_logs for the CI failure")
else:
    warn("Agent did not call get_ci_logs (acceptable — no run ID was provided)")

commit = result.get("likely_commit")
confidence = result.get("confidence", "?")
print(f"\n  Verdict: likely_commit={'null' if not commit else commit.get('sha','')[:8]}"
      f"  confidence={confidence}")
print(f"  Reasoning: {result.get('reasoning','')[:300]}")

if result.get("runbook"):
    ok(f"Runbook attached: {result['runbook'].get('filename')}")
else:
    fail("No runbook in output")

print(f"\n{BOLD}{GREEN}GitHub integration PASSED{RESET} — agent now works on real repo data\n")
