"""Thin wrapper around the Prometheus HTTP API."""
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")


def _window_from_since(since_iso: str | None) -> str:
    """
    Compute a PromQL duration string that covers the time from the alert start
    to now (plus a small pad), so the rate() window matches the actual incident
    duration rather than diluting impact with pre-incident normal traffic.
    """
    if not since_iso:
        return "5m"
    try:
        started = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        # Floor at 45s (3 Prometheus scrape intervals); cap at 10m to avoid huge windows
        seconds = max(45, min(int(elapsed) + 30, 600))
        return f"{seconds}s"
    except Exception:
        return "5m"


def query(promql: str, since: str | None = None) -> float | None:
    """
    Run an instant PromQL query and return the first scalar result, or None.

    If `since` is an ISO timestamp (the alert's starts_at), and the PromQL
    contains the placeholder {window}, it is replaced with a duration string
    computed from the elapsed incident time. This ensures rate() captures the
    full spike without diluting with pre-incident baseline.
    """
    window = _window_from_since(since)
    resolved = promql.replace("{window}", window)
    logger.debug("Prometheus query window=%s promql=%s", window, resolved[:80])

    params = urllib.parse.urlencode({"query": resolved})
    url = f"{PROMETHEUS_URL}/api/v1/query?{params}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        results = data.get("data", {}).get("result", [])
        if not results:
            return None
        value = results[0].get("value", [None, None])[1]
        return float(value) if value is not None else None
    except Exception as exc:
        logger.warning("Prometheus query failed (%s): %s", resolved[:60], exc)
        return None
