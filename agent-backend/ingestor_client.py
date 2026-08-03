"""
Thin client for the Java log-ingestor microservice.

Synchronous helpers (post_log_sync, get_metrics_sync) are safe to call from
background threads. Async wrappers use asyncio.to_thread so they don't block
the FastAPI event loop.

Configure with: LOG_INGESTOR_URL (default: http://log-ingestor:8080)
"""

import asyncio
import json
import logging
import os
import threading
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

_BASE = os.getenv("LOG_INGESTOR_URL", "http://log-ingestor:8080").rstrip("/")
_TIMEOUT = 3  # seconds — never slow down the caller


def is_available() -> bool:
    """True if the ingestor's /health endpoint responds 200."""
    try:
        with urllib.request.urlopen(f"{_BASE}/health", timeout=_TIMEOUT) as r:
            return r.status == 200
    except Exception:
        return False


def post_log_sync(raw_log: str) -> bool:
    """
    POST raw CI log text to /ingest.
    Returns True if the server accepted it (202), False on 429 or any error.
    Called from background threads; never raises.
    """
    try:
        body = raw_log.encode()
        req = urllib.request.Request(
            f"{_BASE}/ingest",
            data=body,
            method="POST",
            headers={"Content-Type": "text/plain; charset=utf-8"},
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return r.status == 202
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            logger.debug("ingestor queue full (429); log dropped")
        else:
            logger.warning("ingestor /ingest returned HTTP %d", exc.code)
        return False
    except Exception as exc:
        logger.debug("ingestor unavailable: %s", exc)
        return False


def get_metrics_sync() -> dict:
    """
    GET /metrics from the ingestor.
    Returns the JSON dict, or {"error": "<reason>"} if unavailable.
    """
    try:
        with urllib.request.urlopen(f"{_BASE}/metrics", timeout=_TIMEOUT) as r:
            return json.loads(r.read())
    except Exception as exc:
        return {"error": str(exc)}


async def get_metrics() -> dict:
    """Async wrapper around get_metrics_sync."""
    return await asyncio.to_thread(get_metrics_sync)


def fire_and_forget(raw_log: str) -> None:
    """
    Dispatch raw_log to the ingestor on a daemon thread.
    Returns immediately; caller is never blocked.
    """
    t = threading.Thread(target=post_log_sync, args=(raw_log,), daemon=True)
    t.start()
