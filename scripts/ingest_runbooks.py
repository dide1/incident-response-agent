#!/usr/bin/env python3
"""
Trigger runbook ingestion via the agent-backend.
Reads all .md files from /runbooks (inside the container), embeds them,
and upserts into pgvector.

Usage: python3 scripts/ingest_runbooks.py [http://localhost:9000]
Safe to re-run — upserts on filename.
"""
import json
import sys
import urllib.error
import urllib.request

BASE_URL = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:9000"

req = urllib.request.Request(
    f"{BASE_URL}/admin/ingest-runbooks",
    data=b"{}",
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read())
except urllib.error.URLError as exc:
    print(f"ERROR: {exc}")
    print(f"Is the agent-backend running at {BASE_URL}?")
    sys.exit(1)

print(f"Ingested {result['count']} runbooks:")
for rb in result["ingested"]:
    print(f"  ✓  {rb['filename']}  —  {rb['title']}")
