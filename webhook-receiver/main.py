import json
import logging
from datetime import datetime, timezone
from fastapi import FastAPI, Request, Response

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Webhook Receiver (Phase 1 — log only)")


@app.post("/webhook")
async def receive_alert(request: Request):
    payload = await request.json()
    received_at = datetime.now(timezone.utc).isoformat()

    alerts = payload.get("alerts", [])
    status = payload.get("status", "unknown")

    logger.info("=" * 70)
    logger.info(f"ALERTMANAGER WEBHOOK  status={status}  alerts={len(alerts)}  at={received_at}")

    for alert in alerts:
        alert_name = alert.get("labels", {}).get("alertname", "?")
        job = alert.get("labels", {}).get("job", "?")
        severity = alert.get("labels", {}).get("severity", "?")
        alert_status = alert.get("status", "?")
        description = alert.get("annotations", {}).get("description", "")
        starts_at = alert.get("startsAt", "")
        logger.info(
            f"  [{alert_status.upper()}] {alert_name}  job={job}  severity={severity}"
        )
        if description:
            logger.info(f"    {description.strip()}")
        if starts_at:
            logger.info(f"    started: {starts_at}")

    logger.info(f"Full payload:\n{json.dumps(payload, indent=2)}")
    logger.info("=" * 70)

    return {"status": "received", "alerts_count": len(alerts), "received_at": received_at}


@app.get("/health")
def health():
    return {"status": "ok", "service": "webhook-receiver"}
