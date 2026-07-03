"""Thin wrapper around the Prometheus HTTP API."""
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")


def query(promql: str) -> float | None:
    """Run an instant PromQL query and return the first scalar result, or None."""
    params = urllib.parse.urlencode({"query": promql})
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
        logger.warning("Prometheus query failed (%s): %s", promql[:60], exc)
        return None
