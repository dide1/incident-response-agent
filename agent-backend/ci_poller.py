"""Background poller: watches configured GitHub repos for CI failures.

Replaces webhooks when the agent runs on localhost (GitHub can't reach it).
Every GITHUB_POLL_INTERVAL seconds (default 120), fetches recent workflow runs
for each repo in GITHUB_REPOS and any repos registered in the DB:

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


def _fetch_recent_runs(repo: str, token: str | None = None) -> list[dict]:
    try:
        data = _get_json(f"/repos/{repo}/actions/runs?per_page=10", token=token)
        return data.get("workflow_runs", [])
    except Exception as exc:
        logger.warning("CI poll failed for %s: %s", repo, exc)
        return []


def _alert_context(
    repo: str, run: dict, user_id: int | None = None, token: str | None = None
) -> dict:
    ctx = {
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
    if user_id is not None:
        ctx["user_id"] = user_id
    if token:
        ctx["github_token"] = token
        ctx["github_repo"] = repo
    return ctx


def _poll_repos(
    repos: list[tuple[str, str, int | None, str | None]],
) -> tuple[list[dict], list[tuple[str, str]]]:
    """
    Poll a list of repos for CI failures.
    repos: [(service, full_repo, user_id, token), ...]
    Returns (new_failure_alert_contexts, resolutions).
    """
    global _seeded
    new_alerts: list[dict] = []
    resolutions: list[tuple[str, str]] = []

    for service, repo, user_id, token in repos:
        for run in _fetch_recent_runs(repo, token):
            if run.get("status") != "completed":
                continue
            key = (repo, run.get("name", ""), run.get("head_branch", ""))
            conclusion = run.get("conclusion")

            if conclusion == "failure" and run["id"] not in _seen_failed_runs:
                _seen_failed_runs.add(run["id"])
                if _seeded:
                    new_alerts.append(_alert_context(repo, run, user_id, token))
                    _open_incidents[key] = True
                    logger.info("CI failure detected: %s run %s", repo, run["id"])

            elif conclusion == "success" and _open_incidents.pop(key, None):
                resolutions.append(("CIFailure", service))
                logger.info("CI recovered: %s workflow '%s' on %s", repo, key[1], key[2])

    _seeded = True
    return new_alerts, resolutions


def _poll_once() -> tuple[list[dict], list[tuple[str, str]]]:
    """Polls env-configured repos only. Used by tests."""
    repos = [
        (svc, full, None, None)
        for svc, full in configured_repos().items()
    ]
    return _poll_repos(repos)


async def poll_github_loop(on_failure, on_resolve) -> None:
    """
    on_failure: async callable(alert_context dict)
    on_resolve: async callable(alertname, service, resolved_at iso str)

    Polls both env-configured repos (GITHUB_REPOS) and DB-registered repos.
    Always runs; skips gracefully each iteration when there is nothing to poll.
    """
    if not os.getenv("GITHUB_TOKEN"):
        logger.warning(
            "CI poller running without GITHUB_TOKEN — unauthenticated GitHub API "
            "is limited to 60 req/hr; set a token or increase GITHUB_POLL_INTERVAL"
        )
    logger.info("CI poller started, interval=%ds", POLL_INTERVAL)

    loop = asyncio.get_event_loop()
    while True:
        try:
            # Env repos (no user association)
            env_repos: dict[str, tuple[str, None, None]] = {
                svc: (full, None, None)
                for svc, full in configured_repos().items()
            }

            # DB repos (per-user token)
            try:
                from db import get_all_repos_with_token
                db_rows = get_all_repos_with_token()
            except Exception:
                db_rows = []

            db_repos: dict[str, tuple[str, int, str]] = {
                row["repo"]: (
                    f"{row['owner']}/{row['repo']}",
                    row["user_id"],
                    row["access_token"],
                )
                for row in db_rows
            }

            # Merge: DB takes precedence for same service name
            merged = {**env_repos, **db_repos}
            repos_list = [
                (svc, full, uid, tok)
                for svc, (full, uid, tok) in merged.items()
            ]

            if not repos_list:
                logger.debug("CI poller: no repos to poll")
            else:
                new_alerts, resolutions = await loop.run_in_executor(
                    None, _poll_repos, repos_list
                )
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
