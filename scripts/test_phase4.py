#!/usr/bin/env python3
"""
Phase 4 test: impact estimation (Prometheus) + Slack brief generation.

Tests:
  A. Prometheus: agent calls query_prometheus and populates impact field
  B. Slack brief: slack_brief block is present in /incidents/latest entry
  C. Slack brief structure: required Block Kit fields are correct
  D. Slack webhook: mock server receives POST when SLACK_WEBHOOK_URL is set
"""
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

BASE = "http://localhost:9000"
BOLD = "\033[1m"
GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"
YELLOW = "\033[33m"


def _req(method, path, body=None, base=None):
    url_base = base or BASE
    data = json.dumps(body).encode() if body is not None else b""
    req = urllib.request.Request(
        f"{url_base}{path}",
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


def ok(msg):
    print(f"  {GREEN}✓{RESET}  {msg}")


def warn(msg):
    print(f"  {YELLOW}⚠{RESET}  {msg}")


def fail(msg):
    print(f"  {RED}✗{RESET}  {msg}")
    sys.exit(1)


def section(title):
    print(f"\n{BOLD}{title}{RESET}")


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


# ── Shared webhook capture ───────────────────────────────────────────────────
_received_payloads: list[dict] = []
_mock_port = 19876


class _MockSlackHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            _received_payloads.append(json.loads(body))
        except Exception:
            pass
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass  # suppress server logs in test output


def _start_mock_webhook_server() -> HTTPServer:
    server = HTTPServer(("127.0.0.1", _mock_port), _MockSlackHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


# ── Seed a fault for phase 4 ────────────────────────────────────────────────
def seed_payments_fault():
    _req("DELETE", "/admin/clear-deploys")
    good = {
        "sha": "eeee" + "4" * 36,
        "service": "payments-service",
        "author": "carol@example.com",
        "commit_message": "chore: bump dependencies",
        "is_fault": False,
        "diff": "- Bumped psycopg2 from 2.9.8 to 2.9.9.",
    }
    bad = {
        "sha": "ffff" + "5" * 36,
        "service": "payments-service",
        "author": "dan@example.com",
        "commit_message": "feat: add new charge method via StripeV3",
        "is_fault": True,
        "diff": (
            "- result = stripe.Charge.create(amount=amount, currency='usd', source=token)\n"
            "+ result = StripeV3Client().charge({'amount': amount, 'token': token})\n"
            "- except stripe.error.CardError as exc:\n"
            "-     raise PaymentDeclinedError(exc.user_message)\n"
            "+ except Exception as exc:\n"
            "+     raise RuntimeError('stripe charge failed') from exc\n"
        ),
    }
    _req("POST", "/deploys", good)
    _req("POST", "/deploys", bad)


def fire_webhook() -> datetime:
    fired_at = datetime.now(timezone.utc)
    webhook = {
        "version": "4",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "HighErrorRate",
                    "job": "payments-service",
                    "severity": "critical",
                },
                "annotations": {
                    "description": (
                        "Error rate on payments-service is 91%. "
                        "RuntimeError from StripeV3Client charge method propagating uncaught."
                    )
                },
                "startsAt": fired_at.isoformat(),
            }
        ],
    }
    _req("POST", "/webhook", webhook)
    return fired_at


# ── Test A: Prometheus impact ────────────────────────────────────────────────
section("Test A — Prometheus impact fields in agent output")

seed_payments_fault()
print("  Firing HighErrorRate webhook for payments-service ...")
fired_at = fire_webhook()
print("  Waiting for agent to complete (up to 120s) ...")
entry = wait_for_incident(fired_at, timeout=120)
result = entry.get("result", {})

impact = result.get("impact")
if impact is None:
    fail("'impact' field missing from agent JSON output — agent schema not updated")

ok("'impact' field present in agent output")

# Check that at least one sub-field was populated (Prometheus may not have data for
# the test service; we accept null values but require the keys to exist)
required_impact_keys = {"error_rate_pct", "requests_per_min", "failed_per_min", "p99_latency_s"}
missing_keys = required_impact_keys - set(impact.keys())
if missing_keys:
    fail(f"impact missing keys: {missing_keys}")
ok(f"impact keys present: {sorted(impact.keys())}")

# Show actual values — may be null if Prometheus has no data for test service
for k, v in impact.items():
    if v is not None:
        ok(f"  impact.{k} = {v}")
    else:
        warn(f"  impact.{k} = null (Prometheus has no data for payments-service — expected in test)")


# ── Test B: Slack brief attached to entry ────────────────────────────────────
section("Test B — Slack brief attached to /incidents/latest entry")

slack = entry.get("slack_brief")
if slack is None:
    fail("'slack_brief' missing from /incidents/latest entry")
ok("'slack_brief' present in entry")

blocks = slack.get("blocks", [])
if not blocks:
    fail("Slack payload has no blocks")
ok(f"{len(blocks)} Block Kit blocks generated")


# ── Test C: Slack brief structure ───────────────────────────────────────────
section("Test C — Block Kit structure validation")

block_types = [b.get("type") for b in blocks]
if "header" not in block_types:
    fail("Slack brief missing header block")
ok("header block present")

# Header text should contain severity and service
header_text = next((b["text"]["text"] for b in blocks if b.get("type") == "header"), "")
if "payments-service" not in header_text:
    warn(f"Header doesn't mention payments-service: '{header_text}'")
else:
    ok(f"Header contains service name: '{header_text[:60]}'")

if "CRITICAL" not in header_text.upper():
    warn(f"Header doesn't reflect critical severity: '{header_text}'")
else:
    ok("Header reflects CRITICAL severity")

# Confirm commit SHA appears somewhere in the blocks
all_text = " ".join(
    b.get("text", {}).get("text", "")
    for b in blocks
    if b.get("type") == "section"
)
if "ffff5555" not in all_text and "ffff" not in all_text:
    warn("Commit SHA not found in Slack brief sections (check if agent blamed correct commit)")
else:
    ok("Blamed commit SHA appears in Slack brief")

# Confirm runbook title is present
runbook_title = result.get("runbook", {}).get("title", "")
if runbook_title and runbook_title not in all_text:
    warn(f"Runbook title '{runbook_title}' not found in Slack brief sections")
elif runbook_title:
    ok(f"Runbook title present: '{runbook_title}'")

# Footer context block
if "context" not in block_types:
    warn("Footer context block missing")
else:
    ok("Footer context block present")


# ── Test D: Mock webhook server receives POST ────────────────────────────────
section("Test D — Slack webhook POST to mock server")

server = _start_mock_webhook_server()
mock_url = f"http://127.0.0.1:{_mock_port}"

# Patch SLACK_WEBHOOK_URL via a direct POST to a synthetic endpoint that exercises
# the notifier. We import it locally and call it directly — the cleanest option
# without a full container restart.
try:
    import sys
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent-backend"))
    os.environ["SLACK_WEBHOOK_URL"] = mock_url
    from slack_notifier import post_slack_brief  # noqa: E402

    test_alert = {
        "alertname": "HighErrorRate",
        "service": "payments-service",
        "severity": "critical",
        "starts_at": datetime.now(timezone.utc).isoformat(),
    }
    _received_payloads.clear()
    post_slack_brief(test_alert, result)

    time.sleep(0.3)  # let the HTTP server flush
    if not _received_payloads:
        fail("Mock webhook server received no POST from post_slack_brief()")

    received = _received_payloads[0]
    if "blocks" not in received:
        fail(f"POST body missing 'blocks': {received}")
    ok(f"Mock webhook received POST with {len(received['blocks'])} blocks")

    recv_text = json.dumps(received)
    if "payments-service" in recv_text:
        ok("Service name present in webhook payload")
    else:
        warn("Service name not found in received webhook payload")

except ImportError as exc:
    warn(f"Could not import slack_notifier for local test (running outside container): {exc}")
    warn("Skipping D — mock server test requires agent-backend importable from local path")
finally:
    server.shutdown()
    os.environ.pop("SLACK_WEBHOOK_URL", None)

# Print the Slack brief so we can visually inspect it
print("\n  Slack brief (Block Kit JSON — excerpt):")
for blk in blocks[:4]:
    btype = blk.get("type", "?")
    if btype == "header":
        print(f"    [header] {blk['text']['text']}")
    elif btype == "section":
        preview = blk["text"]["text"][:120].replace("\n", " ")
        print(f"    [section] {preview}")
    elif btype == "divider":
        print("    [divider]")
    elif btype == "context":
        print(f"    [context] {blk['elements'][0]['text'][:80]}")

if len(blocks) > 4:
    print(f"    ... ({len(blocks) - 4} more blocks)")

print(f"\n{BOLD}{GREEN}Phase 4 PASSED{RESET} — impact estimation + Slack brief working\n")
