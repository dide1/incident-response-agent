"""Format and post Slack incident briefs using Block Kit."""
import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")

_SEVERITY_EMOJI = {"critical": "🔴", "warning": "🟡", "info": "🔵"}


def _fmt_ts(iso: str | None) -> str:
    if not iso:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%H:%M UTC")
    except Exception:
        return iso


def build_slack_blocks(alert: dict, result: dict) -> list[dict]:
    alertname = alert.get("alertname", "Unknown Alert")
    service = alert.get("service", "unknown")
    severity = alert.get("severity", "info")
    emoji = _SEVERITY_EMOJI.get(severity, "⚪")
    confidence = result.get("confidence", "unknown")
    commit = result.get("likely_commit")
    impact = result.get("impact") or {}
    runbook = result.get("runbook") or {}

    # ── Header ──────────────────────────────────────────────────────────────
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{emoji} {severity.upper()}: {alertname} on {service}",
            },
        }
    ]

    # ── Impact strip ────────────────────────────────────────────────────────
    impact_parts = []
    if impact.get("error_rate_pct") is not None:
        impact_parts.append(f"*{impact['error_rate_pct']:.0f}%* error rate")
    if impact.get("failed_per_min") is not None:
        impact_parts.append(f"~{impact['failed_per_min']:.0f} failed req/min")
    if impact.get("p99_latency_s") is not None:
        impact_parts.append(f"P99 latency *{impact['p99_latency_s']:.2f}s*")
    if impact.get("requests_per_min") is not None:
        impact_parts.append(f"{impact['requests_per_min']:.0f} total req/min")

    started = _fmt_ts(alert.get("starts_at"))
    impact_text = ("  •  ".join(impact_parts) if impact_parts else "_metrics unavailable_")
    impact_text += f"  •  since {started}"

    blocks.append({
        "type": "section",
        "text": {"type": "mrkdwn", "text": f":chart_with_upwards_trend: *Impact*\n{impact_text}"},
    })
    blocks.append({"type": "divider"})

    # ── Commit attribution ───────────────────────────────────────────────────
    if commit:
        sha_short = commit.get("sha", "")[:8]
        author = commit.get("author", "unknown")
        message = commit.get("message", "")
        deployed = _fmt_ts(commit.get("deployed_at"))
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":mag: *Likely cause* ({confidence} confidence)\n"
                    f"`{sha_short}`  ·  {author}  ·  deployed {deployed}\n"
                    f"_{message}_"
                ),
            },
        })
        reasoning = result.get("error_match", result.get("reasoning", ""))
        if reasoning:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": reasoning},
            })
    else:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":mag: *Likely cause* ({confidence} confidence)\nNo recent deploys found — may be infrastructure or external dependency.",
            },
        })

    blocks.append({"type": "divider"})

    # ── Runbook ─────────────────────────────────────────────────────────────
    if runbook:
        # Truncate summary to first 2 sentences so the brief stays scannable at 3am.
        # Full procedure is in the linked runbook.
        summary = runbook.get("summary", "")
        sentences = summary.split(". ")
        short_summary = ". ".join(sentences[:2]).strip()
        if not short_summary.endswith("."):
            short_summary += "."
        filename = runbook.get("filename", "")
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":books: *Runbook: {runbook.get('title', 'Unknown')}*\n"
                    f"{short_summary}\n"
                    f"_Full steps: `{filename}`_"
                ),
            },
        })

    # ── Footer ──────────────────────────────────────────────────────────────
    blocks.append({
        "type": "context",
        "elements": [
            {
                "type": "mrkdwn",
                "text": f"incident-response-agent  ·  {alertname}  ·  {service}",
            }
        ],
    })

    return blocks


def post_slack_brief(alert: dict, result: dict) -> dict:
    """
    Build and post a Slack brief. Returns the payload dict (useful for tests).
    If SLACK_WEBHOOK_URL is not set, logs the brief and returns the payload anyway.
    """
    blocks = build_slack_blocks(alert, result)
    payload = {"blocks": blocks}

    if not SLACK_WEBHOOK_URL:
        logger.info(
            "SLACK_WEBHOOK_URL not set — Slack brief (not posted):\n%s",
            json.dumps(payload, indent=2),
        )
        return payload

    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            response_body = resp.read().decode()
            logger.info("Slack brief posted (status=%d body=%s)", resp.status, response_body)
    except urllib.error.URLError as exc:
        logger.error("Failed to post Slack brief: %s", exc)

    return payload


def post_slack_postmortem(incident: dict, postmortem_md: str, resolved_at: str) -> None:
    """Post a postmortem resolved notice to Slack."""
    if not SLACK_WEBHOOK_URL:
        logger.info("SLACK_WEBHOOK_URL not set — postmortem notice not posted")
        return

    alert = incident.get("alert", {})
    result = incident.get("result", {})
    service = alert.get("service", "unknown")
    alertname = alert.get("alertname", "Unknown")
    commit = result.get("likely_commit") or {}
    sha_short = commit.get("sha", "")[:8]

    # First non-heading paragraph of the postmortem as a preview
    preview_lines = [
        l for l in postmortem_md.splitlines()
        if l.strip() and not l.startswith("#") and not l.startswith("**")
    ]
    preview = preview_lines[0][:200] if preview_lines else ""

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"✅ RESOLVED: {alertname} on {service}"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":page_facing_up: *Postmortem auto-generated*\n"
                    f"Root cause: `{sha_short}` — {commit.get('message', '')[:80]}\n"
                    f"{preview}"
                ),
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"incident-response-agent  ·  resolved {_fmt_ts(resolved_at)}"
                        f"  ·  {service}"
                    ),
                }
            ],
        },
    ]

    body = json.dumps({"blocks": blocks}).encode()
    req = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            logger.info("Postmortem Slack notice posted (status=%d)", resp.status)
    except urllib.error.URLError as exc:
        logger.error("Failed to post postmortem Slack notice: %s", exc)
