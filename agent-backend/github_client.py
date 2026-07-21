"""GitHub API client: real commit history, diffs, and CI logs for configured repos.

Configuration (env):
  GITHUB_REPOS  comma-separated owner/repo list, e.g. "dide1/loupe,dide1/incident-response-agent".
                The repo short name (after the /) doubles as the service name in alerts.
  GITHUB_TOKEN  optional PAT; required for private repos, raises rate limits for public ones.
"""
import json
import logging
import os
import pathlib
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"

MAX_DIFF_CHARS = 6000
MAX_LOG_LINES = 150

# Path to the compiled Rust log-analyzer binary.
# Override with LOG_ANALYZER_BIN env var; otherwise look next to the repo root.
_LOG_ANALYZER_BIN = os.getenv("LOG_ANALYZER_BIN") or str(
    pathlib.Path(__file__).parent.parent / "log-analyzer" / "target" / "release" / "log-analyzer"
)


def _analyze_log(log_text: str) -> "dict | str":
    """
    Structured log analysis via Rust binary when available; raw tail otherwise.
    Returns a dict {failed_tests, error_signatures, stack_traces, line_count}
    or a plain string (last MAX_LOG_LINES lines) if the binary isn't present.
    """
    if pathlib.Path(_LOG_ANALYZER_BIN).exists():
        try:
            proc = subprocess.run(
                [_LOG_ANALYZER_BIN, "--all"],
                input=log_text,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if proc.returncode == 0:
                return json.loads(proc.stdout)
            logger.warning("log-analyzer exited %d: %s", proc.returncode, proc.stderr[:200])
        except Exception as exc:
            logger.warning("log-analyzer failed: %s", exc)
    return "\n".join(log_text.splitlines()[-MAX_LOG_LINES:])

# sha -> owner/repo, populated by list_recent_commits so get_commit_diff can
# resolve which repo a sha belongs to without an extra search
_sha_repo_cache: dict[str, str] = {}


def configured_repos() -> dict[str, str]:
    """Map service name (repo short name) -> owner/repo full name."""
    out = {}
    for full in os.getenv("GITHUB_REPOS", "").split(","):
        full = full.strip()
        if full and "/" in full:
            out[full.split("/")[-1]] = full
    return out


def repo_for_service(service: str) -> str | None:
    return configured_repos().get(service)


def _request(
    path_or_url: str,
    accept: str = "application/vnd.github+json",
    token: str | None = None,
) -> bytes:
    url = path_or_url if path_or_url.startswith("http") else f"{GITHUB_API}{path_or_url}"
    headers = {"Accept": accept, "User-Agent": "incident-response-agent"}
    effective_token = token or os.getenv("GITHUB_TOKEN", "")
    if effective_token:
        headers["Authorization"] = f"Bearer {effective_token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read()


def _get_json(path: str, token: str | None = None):
    return json.loads(_request(path, token=token))


def list_recent_commits(
    service: str,
    window_minutes: int,
    branch: str | None = None,
    github_repo: str | None = None,
    token: str | None = None,
) -> list[dict]:
    """
    Real commits from the configured repo, shaped like deploy_tracker rows so the
    agent's get_recent_deploys tool interface is unchanged.

    branch: the GitHub commits API defaults to the default branch, which silently
    hides commits on feature branches — the exact commits CI failures usually
    point at. Passing the branch from the alert scopes the suspect pool correctly.

    Falls back to the last 5 commits when the window is empty (real repos often
    have no pushes in the last 90 minutes) — deployed_at stays honest so the
    agent can see they are older than the alert.
    """
    repo = github_repo or repo_for_service(service)
    if not repo:
        return []

    branch_param = f"&sha={urllib.parse.quote(branch, safe='')}" if branch else ""
    since = (datetime.now(timezone.utc) - timedelta(minutes=window_minutes)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    try:
        commits = _get_json(
            f"/repos/{repo}/commits?since={since}&per_page=20{branch_param}", token=token
        )
        if not commits:
            commits = _get_json(
                f"/repos/{repo}/commits?per_page=5{branch_param}", token=token
            )
            logger.info(
                "No commits in %dm window for %s (branch=%s) — returning last %d commits",
                window_minutes, repo, branch or "default", len(commits),
            )
    except urllib.error.HTTPError as exc:
        logger.error("GitHub commits fetch failed for %s: %s", repo, exc)
        return [{"error": f"GitHub API error for {repo}: HTTP {exc.code}"}]

    rows = []
    for c in commits:
        sha = c["sha"]
        _sha_repo_cache[sha] = repo
        commit = c.get("commit", {})
        author = commit.get("author") or {}
        rows.append({
            "sha": sha,
            "service": service,
            "deployed_at": author.get("date", ""),
            "author": author.get("email", author.get("name", "unknown")),
            "commit_message": commit.get("message", "").split("\n")[0],
            "branch": branch or "default",
        })
    return rows


def fetch_commit_diff(sha: str, token: str | None = None) -> dict | None:
    """Real diff for a sha, from the cache-resolved repo or by trying each configured repo."""
    candidates = []
    if sha in _sha_repo_cache:
        candidates.append(_sha_repo_cache[sha])
    candidates.extend(r for r in configured_repos().values() if r not in candidates)

    for repo in candidates:
        try:
            raw = _request(
                f"/repos/{repo}/commits/{sha}",
                accept="application/vnd.github.diff",
                token=token,
            ).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 422):
                continue
            logger.error("GitHub diff fetch failed for %s@%s: %s", repo, sha[:8], exc)
            continue

        if len(raw) > MAX_DIFF_CHARS:
            raw = raw[:MAX_DIFF_CHARS] + f"\n... [diff truncated, {len(raw) - MAX_DIFF_CHARS} more chars]"
        return {"sha": sha, "service": repo.split("/")[-1], "diff": raw}

    return None


def fetch_open_prs(repo: str, token: str | None = None) -> list[dict]:
    """Return open PRs: number, title, head sha, branch, author, url."""
    try:
        data = _get_json(f"/repos/{repo}/pulls?state=open&per_page=20", token=token)
    except urllib.error.HTTPError as exc:
        logger.warning("PR fetch failed for %s: %s", repo, exc)
        return []
    return [
        {
            "number": pr["number"],
            "title": pr["title"],
            "head_sha": pr["head"]["sha"],
            "branch": pr["head"]["ref"],
            "author": pr["user"]["login"],
            "html_url": pr["html_url"],
        }
        for pr in data
    ]


def fetch_pr_diff(repo: str, pr_number: int, token: str | None = None) -> str:
    """Unified diff for a pull request."""
    try:
        raw = _request(
            f"/repos/{repo}/pulls/{pr_number}",
            accept="application/vnd.github.diff",
            token=token,
        ).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return f"[diff fetch failed: HTTP {exc.code}]"
    if len(raw) > MAX_DIFF_CHARS:
        raw = raw[:MAX_DIFF_CHARS] + f"\n... [diff truncated, {len(raw) - MAX_DIFF_CHARS} more chars]"
    return raw


def post_pr_comment(repo: str, pr_number: int, body: str, token: str | None = None) -> None:
    """Post a comment on a GitHub PR."""
    url = f"{GITHUB_API}/repos/{repo}/issues/{pr_number}/comments"
    payload = json.dumps({"body": body}).encode()
    headers = {
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "incident-response-agent",
    }
    effective_token = token or os.getenv("GITHUB_TOKEN", "")
    if effective_token:
        headers["Authorization"] = f"Bearer {effective_token}"
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        logger.error("Failed to post PR comment on %s#%d: %s", repo, pr_number, exc)


def fetch_ci_logs(
    service: str,
    run_id: int | None = None,
    github_repo: str | None = None,
    token: str | None = None,
) -> dict:
    """
    Failed-job log tails for a GitHub Actions run. When run_id is omitted,
    uses the most recent failed run in the repo.
    """
    repo = github_repo or repo_for_service(service)
    if not repo:
        return {"error": f"No GitHub repo configured for service '{service}'"}

    try:
        if run_id is None:
            runs = _get_json(
                f"/repos/{repo}/actions/runs?status=failure&per_page=1", token=token
            )
            if not runs.get("workflow_runs"):
                return {"error": f"No failed workflow runs found in {repo}"}
            run = runs["workflow_runs"][0]
            run_id = run["id"]
        else:
            run = _get_json(f"/repos/{repo}/actions/runs/{run_id}", token=token)

        jobs = _get_json(
            f"/repos/{repo}/actions/runs/{run_id}/jobs", token=token
        ).get("jobs", [])
        failed_jobs = [j for j in jobs if j.get("conclusion") == "failure"]

        results = []
        for job in failed_jobs[:3]:
            failed_steps = [
                s["name"] for s in job.get("steps", []) if s.get("conclusion") == "failure"
            ]
            entry = {"job": job["name"], "failed_steps": failed_steps, "log_tail": ""}
            try:
                log_text = _request(
                    f"/repos/{repo}/actions/jobs/{job['id']}/logs", token=token
                ).decode("utf-8", errors="replace")
                analysis = _analyze_log(log_text)
                if isinstance(analysis, dict):
                    entry["log_analysis"] = analysis
                else:
                    entry["log_tail"] = analysis
            except urllib.error.HTTPError as exc:
                entry["log_tail"] = f"[log fetch failed: HTTP {exc.code}]"
            results.append(entry)

        return {
            "run_id": run_id,
            "workflow": run.get("name", ""),
            "head_sha": run.get("head_sha", ""),
            "head_branch": run.get("head_branch", ""),
            "conclusion": run.get("conclusion", ""),
            "html_url": run.get("html_url", ""),
            "failed_jobs": results,
        }
    except urllib.error.HTTPError as exc:
        logger.error("GitHub CI logs fetch failed for %s: %s", repo, exc)
        return {"error": f"GitHub API error for {repo}: HTTP {exc.code}"}
