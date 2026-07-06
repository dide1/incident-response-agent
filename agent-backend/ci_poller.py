"""Background poller: watches configured GitHub repos for CI failures.

Replaces webhooks when the agent runs on localhost (GitHub can't reach it).
Every GITHUB_POLL_INTERVAL seconds (default 120), fetches recent workflow runs
for each repo in GITHUB_REPOS:

  - a newly seen failed run  → fires the agent (CIFailure incident)
  - a success on a workflow+branch we previously alerted on → triggers
    postmortem generation (the incident is considered resolved)

The first poll only records existing failures without alerting, so restarting
the stack doesn't replay old incidents.
"""
import asyncio
import logging
import os
from datetime import datetime, timezone

from github_client import _get_json, configured_repos

logger = logging.getLogger(__name__)

POLL_INTERVAL = int(os.getenv("GITHUB_POLL_INTERVAL", "120"))

_seen_failed_runs: set[int] = set()
# (repo, workflow name, branch) -> True while an alerted failure awaits resolution
_open_incidents: dict[tuple[str, str, str], bool] = {}
_seeded = False


def _fetch_recent_runs(repo: str) -> list[dict]:
    try:
        data = _get_json(f"/repos/{repo}/actions/runs?per_page=10")
        return data.get("workflow_runs", [])
    except Exception as exc:
        logger.warning("CI poll failed for %s: %s", repo, exc)
        return []


def _alert_context(repo: str, run: dict) -> dict:
    return {
        "alertname": "CIFailure",
        "service": repo.split("/")[-1],
        "severity": "warning",
        "description": (
            f"GitHub Actions workflow '{run.get('name')}' failed on branch "
            f"{run.get('head_branch')} at commit {run.get('head_sha', '')[:8]}. "
            f"Run ID: {run.get('id')}. URL: {run.get('html_url')}"
        ),
        "starts_at": run.get("run_started_at"),
    }


def _poll_once() -> tuple[list[dict], list[tuple[str, str]]]:
    """
    Returns (new_failure_alert_contexts, resolutions) where resolutions are
    (alertname, service) pairs whose failing workflow has gone green.
    """
    global _seeded
    new_alerts: list[dict] = []
    resolutions: list[tuple[str, str]] = []

    for service, repo in configured_repos().items():
        for run in _fetch_recent_runs(repo):
            if run.get("status") != "completed":
                continue
            key = (repo, run.get("name", ""), run.get("head_branch", ""))
            conclusion = run.get("conclusion")

            if conclusion == "failure" and run["id"] not in _seen_failed_runs:
                _seen_failed_runs.add(run["id"])
                if _seeded:
                    new_alerts.append(_alert_context(repo, run))
                    _open_incidents[key] = True
                    logger.info("CI failure detected: %s run %s", repo, run["id"])

            elif conclusion == "success" and _open_incidents.pop(key, None):
                resolutions.append(("CIFailure", service))
                logger.info("CI recovered: %s workflow '%s' on %s", repo, key[1], key[2])

    _seeded = True
    return new_alerts, resolutions


async def poll_github_loop(on_failure, on_resolve) -> None:
    """
    on_failure: async callable(alert_context dict)
    on_resolve: async callable(alertname, service, resolved_at iso str)
    """
    repos = configured_repos()
    if not repos:
        logger.info("CI poller disabled — GITHUB_REPOS not set")
        return
    if not os.getenv("GITHUB_TOKEN"):
        logger.warning(
            "CI poller running without GITHUB_TOKEN — unauthenticated GitHub API "
            "is limited to 60 req/hr; set a token or increase GITHUB_POLL_INTERVAL"
        )
    logger.info(
        "CI poller started: %s every %ds", ", ".join(repos.values()), POLL_INTERVAL
    )

    loop = asyncio.get_event_loop()
    while True:
        try:
            new_alerts, resolutions = await loop.run_in_executor(None, _poll_once)
            for ctx in new_alerts:
                await on_failure(ctx)
            for alertname, service in resolutions:
                await on_resolve(
                    alertname, service, datetime.now(timezone.utc).isoformat()
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("CI poller iteration failed")
        await asyncio.sleep(POLL_INTERVAL)
