import asyncio
import json
import logging
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

import os
import pathlib

from agent import run_agent
from db import (
    clear_deploys, init_db, insert_deploy, insert_incident, insert_postmortem,
    get_latest_postmortem, list_deploys, list_incidents, list_postmortems,
    list_runbooks_db, upsert_runbook,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Keeps the last 20 completed analyses in memory for test querying
_recent_analyses: deque = deque(maxlen=20)


def _ingest_runbooks_sync() -> list[dict]:
    """Read, embed, and upsert every runbook. Shared by startup and the admin endpoint."""
    from embedder import embed

    runbooks_dir = pathlib.Path(os.getenv("RUNBOOKS_DIR", "/runbooks"))
    if not runbooks_dir.exists():
        raise FileNotFoundError(f"Runbooks directory not found: {runbooks_dir}")

    ingested = []
    for md_file in sorted(runbooks_dir.glob("*.md")):
        content = md_file.read_text(encoding="utf-8")
        title_line = next((ln for ln in content.splitlines() if ln.startswith("# ")), None)
        title = title_line[2:].strip() if title_line else md_file.stem
        embedding = embed(f"{title}\n\n{content}")
        upsert_runbook(md_file.name, title, content, embedding)
        ingested.append({"filename": md_file.name, "title": title})
    return ingested


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()

    # Auto-ingest runbooks so `docker compose up` is the whole setup
    loop = asyncio.get_event_loop()
    try:
        ingested = await loop.run_in_executor(None, _ingest_runbooks_sync)
        logger.info("Startup runbook ingestion: %d runbooks", len(ingested))
    except Exception as exc:
        logger.warning("Startup runbook ingestion skipped: %s", exc)

    # CI poller (no-op when GITHUB_REPOS is unset)
    from ci_poller import poll_github_loop
    poller = asyncio.create_task(
        poll_github_loop(_run_agent_background, _generate_postmortem_background)
    )

    yield
    poller.cancel()


app = FastAPI(title="Incident Response Agent", lifespan=lifespan)


@app.post("/webhook")
async def receive_alert(payload: dict):
    """Receives Alertmanager webhooks. Returns 200 immediately; agent runs in background."""
    alerts = payload.get("alerts", [])
    status = payload.get("status", "unknown")
    logger.info("Webhook received: status=%s alerts=%d", status, len(alerts))

    for alert in alerts:
        alertname = alert["labels"].get("alertname")
        service = alert["labels"].get("job")
        alert_status = alert.get("status")

        if alert_status == "firing":
            alert_context = {
                "alertname": alertname,
                "service": service,
                "severity": alert["labels"].get("severity"),
                "description": alert.get("annotations", {}).get("description", ""),
                "starts_at": alert.get("startsAt"),
            }
            asyncio.create_task(_run_agent_background(alert_context))

        elif alert_status == "resolved":
            resolved_at = alert.get("endsAt") or datetime.now(timezone.utc).isoformat()
            logger.info("Alert resolved: %s on %s at %s", alertname, service, resolved_at)
            asyncio.create_task(_generate_postmortem_background(alertname, service, resolved_at))

        else:
            logger.info("Skipping alert with status=%s", alert_status)

    return {"status": "received", "alerts_count": len(alerts)}


async def _run_agent_background(alert: dict) -> None:
    service = alert.get("service", "unknown")
    alertname = alert.get("alertname", "unknown")
    logger.info("Agent starting for %s on %s", alertname, service)
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, run_agent, alert)

        # Post Slack brief (no-op when SLACK_WEBHOOK_URL is unset)
        from slack_notifier import post_slack_brief
        slack_payload = await loop.run_in_executor(None, post_slack_brief, alert, result)

        entry = {
            "alert": alert,
            "result": result,
            "slack_brief": slack_payload,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        _recent_analyses.append(entry)

        try:
            insert_incident(alert, result)
        except Exception:
            logger.exception("Failed to persist incident to DB")

        logger.info("=" * 70)
        logger.info("AGENT ANALYSIS  alert=%s  service=%s", alertname, service)
        logger.info(json.dumps(result, indent=2, default=str))
        logger.info("=" * 70)
    except Exception:
        logger.exception("Agent failed for alert=%s service=%s", alertname, service)


@app.post("/webhook/github")
async def receive_github_webhook(request: Request):
    """
    GitHub webhook receiver.
    - workflow_run (completed, conclusion=failure) → run the agent as a CIFailure incident
    - push → record commits into deploy_tracker (deploy history for the repo)
    Verifies X-Hub-Signature-256 when GITHUB_WEBHOOK_SECRET is set.
    """
    import hashlib
    import hmac

    body = await request.body()
    secret = os.getenv("GITHUB_WEBHOOK_SECRET", "")
    if secret:
        signature = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

    event = request.headers.get("X-GitHub-Event", "")
    payload = json.loads(body) if body else {}
    repo_name = payload.get("repository", {}).get("name", "unknown")

    if event == "workflow_run":
        run = payload.get("workflow_run", {})
        if payload.get("action") == "completed" and run.get("conclusion") == "failure":
            alert_context = {
                "alertname": "CIFailure",
                "service": repo_name,
                "severity": "warning",
                "description": (
                    f"GitHub Actions workflow '{run.get('name')}' failed on branch "
                    f"{run.get('head_branch')} at commit {run.get('head_sha', '')[:8]}. "
                    f"Run ID: {run.get('id')}. URL: {run.get('html_url')}"
                ),
                "starts_at": run.get("run_started_at"),
            }
            logger.info("CI failure webhook: %s run %s", repo_name, run.get("id"))
            asyncio.create_task(_run_agent_background(alert_context))
            return {"status": "investigating", "run_id": run.get("id")}
        return {"status": "ignored", "reason": f"workflow_run {payload.get('action')}/{run.get('conclusion')}"}

    if event == "push":
        commits = payload.get("commits", [])
        for c in commits:
            insert_deploy({
                "sha": c["id"],
                "service": repo_name,
                "author": c.get("author", {}).get("email", "unknown"),
                "commit_message": c.get("message", "").split("\n")[0],
                "deployed_at": c.get("timestamp"),
                "branch": payload.get("ref", "").replace("refs/heads/", ""),
            })
        logger.info("Push webhook: recorded %d commits for %s", len(commits), repo_name)
        return {"status": "recorded", "commits": len(commits)}

    return {"status": "ignored", "event": event}


async def _generate_postmortem_background(alertname: str, service: str, resolved_at: str) -> None:
    """Find the matching firing incident and generate a postmortem."""
    from postmortem import generate_postmortem
    from slack_notifier import post_slack_postmortem

    # Find the most recent completed incident for this alertname + service
    incident = None
    for entry in reversed(list(_recent_analyses)):
        a = entry.get("alert", {})
        if a.get("alertname") == alertname and a.get("service") == service:
            incident = entry
            break

    if not incident:
        logger.warning(
            "Resolved webhook for %s/%s but no matching firing incident found in memory",
            alertname, service,
        )
        return

    logger.info("Generating postmortem for %s on %s", alertname, service)
    try:
        loop = asyncio.get_event_loop()
        content = await loop.run_in_executor(None, generate_postmortem, incident, resolved_at)
        insert_postmortem(alertname, service, content, incident)
        await loop.run_in_executor(None, post_slack_postmortem, incident, content, resolved_at)
        logger.info("Postmortem stored and posted for %s/%s", alertname, service)
    except Exception:
        logger.exception("Postmortem generation failed for %s/%s", alertname, service)


@app.post("/deploys", status_code=201)
async def record_deploy(deploy: dict):
    """Record a deployment. Called by inject_fault.sh / seed_deploys.py."""
    required = {"sha", "service", "author", "commit_message"}
    missing = required - deploy.keys()
    if missing:
        raise HTTPException(status_code=422, detail=f"Missing fields: {missing}")
    insert_deploy(deploy)
    logger.info("Deploy recorded: sha=%s service=%s", deploy["sha"][:8], deploy["service"])
    return {"status": "recorded", "sha": deploy["sha"]}


@app.get("/deploys")
async def get_deploys(service: str = None, limit: int = 20):
    return list_deploys(service=service, limit=limit)


@app.get("/incidents/latest")
async def get_latest_incident():
    """Returns the most recent completed agent analysis. Used by test scripts."""
    if not _recent_analyses:
        return {"status": "no_analysis_yet"}
    return list(_recent_analyses)[-1]


@app.delete("/admin/clear-deploys", status_code=204)
async def admin_clear_deploys():
    """Truncate deploy tables. Test setup only."""
    clear_deploys()
    logger.info("Deploy tables cleared (admin)")


@app.post("/admin/ingest-runbooks", status_code=200)
async def ingest_runbooks():
    """Re-embed and upsert all runbooks. Also runs automatically at startup."""
    try:
        loop = asyncio.get_event_loop()
        ingested = await loop.run_in_executor(None, _ingest_runbooks_sync)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    logger.info("Runbook ingestion complete: %d files", len(ingested))
    return {"ingested": ingested, "count": len(ingested)}


@app.get("/runbooks")
async def list_runbooks():
    return list_runbooks_db()


@app.post("/runbooks/search")
async def search_runbooks_endpoint(body: dict):
    """Direct vector search endpoint for probing/debugging retrieval quality."""
    from embedder import embed
    from db import search_runbooks_db
    query = body.get("query", "")
    if not query:
        raise HTTPException(status_code=422, detail="query field required")
    top_k = int(body.get("top_k", 3))
    vec = embed(query)
    return search_runbooks_db(vec, top_k=top_k)


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Serve the incident dashboard."""
    page = pathlib.Path(__file__).parent / "static" / "index.html"
    if not page.exists():
        return HTMLResponse("<h1>Dashboard not found</h1>", status_code=404)
    return HTMLResponse(page.read_text(encoding="utf-8"))


@app.get("/incidents")
async def get_incidents(limit: int = 50):
    """Persisted incident history, newest first."""
    return list_incidents(limit=limit)


@app.get("/postmortems")
async def get_postmortems(limit: int = 20):
    """Persisted postmortems, newest first."""
    return list_postmortems(limit=limit)


@app.get("/postmortems/latest")
async def get_latest_postmortem_endpoint():
    """Returns the most recently generated postmortem."""
    row = get_latest_postmortem()
    if not row:
        return {"status": "no_postmortem_yet"}
    return row


@app.post("/admin/generate-postmortem")
async def admin_generate_postmortem(body: dict):
    """
    Manually trigger postmortem generation for the latest incident matching
    alertname + service. Used by tests that don't wait for Alertmanager to resolve.
    Body: {"alertname": "...", "service": "...", "resolved_at": "<ISO>"}
    """
    alertname = body.get("alertname")
    service = body.get("service")
    resolved_at = body.get("resolved_at") or datetime.now(timezone.utc).isoformat()
    if not alertname or not service:
        raise HTTPException(status_code=422, detail="alertname and service required")
    asyncio.create_task(_generate_postmortem_background(alertname, service, resolved_at))
    return {"status": "postmortem_generation_started"}


@app.get("/health")
def health():
    return {"status": "ok", "service": "agent-backend"}
