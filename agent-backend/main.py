import asyncio
import json
import logging
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException

from agent import run_agent
from db import clear_deploys, init_db, insert_deploy, list_deploys

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
        if alert.get("status") != "firing":
            logger.info("Skipping non-firing alert (status=%s)", alert.get("status"))
            continue

        alert_context = {
            "alertname": alert["labels"].get("alertname"),
            "service": alert["labels"].get("job"),
            "severity": alert["labels"].get("severity"),
            "description": alert.get("annotations", {}).get("description", ""),
            "starts_at": alert.get("startsAt"),
        }
        asyncio.create_task(_run_agent_background(alert_context))

    return {"status": "received", "alerts_count": len(alerts)}


async def _run_agent_background(alert: dict) -> None:
    service = alert.get("service", "unknown")
    alertname = alert.get("alertname", "unknown")
    logger.info("Agent starting for %s on %s", alertname, service)
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, run_agent, alert)

        entry = {
            "alert": alert,
            "result": result,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        _recent_analyses.append(entry)

        logger.info("=" * 70)
        logger.info("AGENT ANALYSIS  alert=%s  service=%s", alertname, service)
        logger.info(json.dumps(result, indent=2, default=str))
        logger.info("=" * 70)
    except Exception:
        logger.exception("Agent failed for alert=%s service=%s", alertname, service)


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
    """Truncate deploy tables. Test setup only — not for production use."""
    clear_deploys()
    logger.info("Deploy tables cleared (admin)")


@app.get("/health")
def health():
    return {"status": "ok", "service": "agent-backend"}
