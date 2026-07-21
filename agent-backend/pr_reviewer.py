"""Background poller: reviews new/updated PRs for bugs without needing test failures.

Every GITHUB_POLL_INTERVAL seconds, fetches open PRs for each registered repo.
Tracks by (repo, pr_number, head_sha) so a new commit to an existing PR triggers
a fresh review. Posts findings as a PR comment and Slack notification (critical/high only).
"""
import asyncio
import logging
import os

from github_client import fetch_open_prs, fetch_pr_diff, post_pr_comment

logger = logging.getLogger(__name__)

POLL_INTERVAL = int(os.getenv("GITHUB_POLL_INTERVAL", "120"))

# (repo, pr_number, head_sha) — reviewed at this exact commit, skip until head changes
_reviewed: set[tuple[str, int, str]] = set()


def _format_pr_comment(result: dict) -> str:
    findings = result.get("findings", [])
    safe = result.get("safe_to_merge", True)
    summary = result.get("summary", "")

    emoji_map = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}
    lines = ["## 🤖 Automated PR Review\n"]

    if not findings:
        lines.append(f"✅ No issues found. {summary}")
        return "\n".join(lines)

    status = "🚫 **Not safe to merge**" if not safe else "⚠️ **Review findings below**"
    lines.append(f"{status}\n\n{summary}\n")

    severity_order = ["critical", "high", "medium", "low"]
    grouped: dict[str, list[dict]] = {s: [] for s in severity_order}
    for f in findings:
        grouped.setdefault(f.get("severity", "low"), []).append(f)

    for severity in severity_order:
        for f in grouped[severity]:
            em = emoji_map.get(severity, "⚪")
            lines.append(
                f"### {em} {severity.upper()}: {f.get('title', '')}\n"
                f"**File:** `{f.get('file', 'unknown')}`\n\n"
                f"{f.get('description', '')}\n\n"
                f"**Fix:** {f.get('fix', '')}\n"
            )

    lines.append("\n---\n_incident-response-agent PR reviewer_")
    return "\n".join(lines)


def _review_pr(repo: str, pr: dict, token: str | None) -> dict:
    from agent import run_pr_review
    diff = fetch_pr_diff(repo, pr["number"], token=token)
    return run_pr_review(pr, diff)


def _poll_prs(
    repos: list[tuple[str, str, int | None, str | None]],
) -> list[tuple[str, str, dict, dict]]:
    """
    repos: [(service, full_repo, user_id, token), ...]
    Returns list of (repo, full_repo, pr_info, review_result) for new reviews.
    """
    new_reviews = []
    for _service, repo, _user_id, token in repos:
        prs = fetch_open_prs(repo, token=token)
        for pr in prs:
            key = (repo, pr["number"], pr["head_sha"])
            if key in _reviewed:
                continue
            _reviewed.add(key)
            logger.info("PR reviewer: reviewing %s#%d (%s)", repo, pr["number"], pr["head_sha"][:8])
            try:
                result = _review_pr(repo, pr, token)
                new_reviews.append((repo.split("/")[-1], repo, pr, result))
            except Exception:
                logger.exception("PR review failed for %s#%d", repo, pr["number"])
    return new_reviews


async def poll_pr_loop(on_review) -> None:
    """
    on_review: async callable(service, repo, pr_info, result)
    Polls both env-configured repos and DB-registered repos.
    """
    logger.info("PR reviewer started, interval=%ds", POLL_INTERVAL)
    loop = asyncio.get_event_loop()

    while True:
        try:
            from github_client import configured_repos
            env_repos = [(svc, full, None, None) for svc, full in configured_repos().items()]

            try:
                from db import get_all_repos_with_token
                db_rows = get_all_repos_with_token()
            except Exception:
                db_rows = []

            db_repos = {
                row["repo"]: (f"{row['owner']}/{row['repo']}", row["user_id"], row["access_token"])
                for row in db_rows
            }

            merged: dict[str, tuple] = {svc: (full, None, None) for svc, full in configured_repos().items()}
            for svc, (full, uid, tok) in db_repos.items():
                merged[svc] = (full, uid, tok)

            repos_list = [(svc, full, uid, tok) for svc, (full, uid, tok) in merged.items()]

            if repos_list:
                new_reviews = await loop.run_in_executor(None, _poll_prs, repos_list)
                for service, repo, pr, result in new_reviews:
                    await on_review(service, repo, pr, result)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("PR reviewer iteration failed")

        await asyncio.sleep(POLL_INTERVAL)
