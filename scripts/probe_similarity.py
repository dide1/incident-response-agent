#!/usr/bin/env python3
"""
Adversarial similarity probe — NOT part of the test suite.
Run this after ingest to inspect what the vector search actually returns
for each query, so we can reason about retrieval quality before phase 4.

Usage: python3 scripts/probe_similarity.py
"""
import json
import sys
import urllib.request
import urllib.error

BASE = "http://localhost:9000"

PROBES = [
    {
        "label": "Payments HighErrorRate (canonical)",
        "query": "PaymentGatewayError propagating uncaught on payments-service high error rate",
    },
    {
        "label": "Missing retry logic (same symptom, different cause)",
        "query": "removed retry loop payment gateway transient failures now propagate",
    },
    {
        "label": "Adversarial: DB connection pool exhaustion",
        "query": "database connection pool exhausted requests timing out service returning 503",
    },
    {
        "label": "Adversarial: downstream service timeout (SIMILAR symptoms to pool exhaustion)",
        "query": "downstream service unavailable upstream timeout connection refused cascading failure",
    },
    {
        "label": "N+1 latency spike (order-service)",
        "query": "N+1 query per-row database fetch order listing endpoint latency spike 3 seconds",
    },
    {
        "label": "Memory leak (latency spike, different cause)",
        "query": "service memory usage growing latency increasing over time restarts clear it",
    },
    {
        "label": "Bad deploy rollback",
        "query": "recent deploy introduced regression need to rollback to previous version",
    },
]


def search(query: str) -> list[dict]:
    body = json.dumps({"query": query}).encode()
    req = urllib.request.Request(
        f"{BASE}/runbooks/search",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return [{"error": f"HTTP {exc.code}"}]
    except urllib.error.URLError as exc:
        return [{"error": str(exc)}]


print("\n" + "=" * 70)
print("RUNBOOK SIMILARITY PROBE")
print("=" * 70)
for probe in PROBES:
    print(f"\n[{probe['label']}]")
    print(f"  Query: {probe['query'][:80]}...")
    hits = search(probe["query"])
    for i, hit in enumerate(hits[:3], 1):
        if "error" in hit:
            print(f"  {i}. ERROR: {hit['error']}")
        else:
            score = hit.get("similarity", "?")
            filename = hit.get("filename", "?")
            title = hit.get("title", "?")
            print(f"  {i}. [{score:.3f}]  {filename}  —  {title}")

print("\n" + "=" * 70)
print("Probe complete. Check rankings above for:")
print("  - Correct top-1 for each canonical scenario")
print("  - Score gap between #1 and #2 (>0.05 = confident separation)")
print("  - Adversarial pair: pool-exhaustion vs downstream-timeout (#3 vs #4)")
print("=" * 70 + "\n")
