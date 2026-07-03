import asyncio
import json
import logging
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException

import os
import pathlib

from agent import run_agent
from db import (
    clear_deploys, init_db, insert_deploy, insert_postmortem,
    get_latest_postmortem, list_deploys, list_runbooks_db, upsert_runbook,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Keeps the last 20 completed analyses in memory for test querying
_recent_analyses: deque = deque(maxlen=20)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


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

        logger.info("=" * 70)
        logger.info("AGENT ANALYSIS  alert=%s  service=%s", alertname, service)
        logger.info(json.dumps(result, indent=2, default=str))
        logger.info("=" * 70)
    except Exception:
        logger.exception("Agent failed for alert=%s service=%s", alertname, service)


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
    """
    Read all .md files from /runbooks, embed them, and upsert into pgvector.
    Safe to call multiple times (upserts on filename).
    """
    from embedder import embed

    runbooks_dir = pathlib.Path(os.getenv("RUNBOOKS_DIR", "/runbooks"))
    if not runbooks_dir.exists():
        raise HTTPException(status_code=500, detail=f"Runbooks directory not found: {runbooks_dir}")

    ingested = []
    for md_file in sorted(runbooks_dir.glob("*.md")):
        content = md_file.read_text(encoding="utf-8")
        title_line = next((l for l in content.splitlines() if l.startswith("# ")), None)
        title = title_line[2:].strip() if title_line else md_file.stem

        # Embed title + content together so queries on either surface the runbook
        embedding = embed(f"{title}\n\n{content}")
        upsert_runbook(md_file.name, title, content, embedding)
        ingested.append({"filename": md_file.name, "title": title})
        logger.info("Ingested runbook: %s", md_file.name)

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
