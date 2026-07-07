"""Generate Markdown postmortems from completed incident analysis."""
import json
import logging
from datetime import datetime

import anthropic

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an SRE writing a production incident postmortem.

Given the incident data below, output a complete postmortem in Markdown.
Required sections (use these exact headings):
  ## Executive Summary
  ## Timeline          (Markdown table: | Time (UTC) | Event |)
  ## Root Cause
  ## Impact
  ## Detection & Response
  ## Action Items      (GitHub-style checkboxes: - [ ] ...)
  ## Contributing Factors
  ## Lessons Learned

Rules:
- Output ONLY the Markdown document. No prose before or after.
- Start with a level-1 heading: # Postmortem: <alertname> — <service>
- Follow it immediately with: **Date:** ... · **Duration:** ... · **Severity:** ... · **Status:** Resolved
- Be specific: cite the exact commit SHA (first 8 chars), author, and the specific code change.
- Timeline must include: commit deployed, alert fired, root cause identified, Slack brief posted, alert resolved.
- Action items must be concrete, specific, and assignable — not generic advice.
- Duration is resolved_at minus alert starts_at.
- If impact metrics are null, omit them rather than writing "null"."""


def _fmt_ts(iso: str | None, fmt: str = "%H:%M UTC") -> str:
    if not iso:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime(fmt)
    except Exception:
        return str(iso)


def _duration_str(start_iso: str | None, end_iso: str | None) -> str:
    if not start_iso or not end_iso:
        return "unknown"
    try:
        start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        secs = int((end - start).total_seconds())
        if secs < 60:
            return f"{secs}s"
        return f"{secs // 60}m {secs % 60}s"
    except Exception:
        return "unknown"


def generate_postmortem(incident_entry: dict, resolved_at: str) -> str:
    """
    Call Claude once (no tools) to write a Markdown postmortem.
    Returns the raw Markdown string.
    """
    alert = incident_entry.get("alert", {})
    result = incident_entry.get("result", {})
    completed_at = incident_entry.get("completed_at", "")
    commit = result.get("likely_commit") or {}

    context = {
        "alert": {
            "alertname": alert.get("alertname"),
            "service": alert.get("service"),
            "severity": alert.get("severity"),
            "description": alert.get("description"),
            "starts_at": alert.get("starts_at"),
        },
        "analysis": {
            "likely_commit": commit,
            "confidence": result.get("confidence"),
            "reasoning": result.get("reasoning"),
            "error_match": result.get("error_match"),
            "impact": result.get("impact"),
            "runbook_title": (result.get("runbook") or {}).get("title"),
            "runbook_summary": (result.get("runbook") or {}).get("summary"),
        },
        "timeline": {
            "commit_deployed_at": commit.get("deployed_at"),
            "alert_fired_at": alert.get("starts_at"),
            "root_cause_identified_at": completed_at,
            "slack_brief_posted_at": completed_at,
            "resolved_at": resolved_at,
        },
        "computed": {
            "incident_duration": _duration_str(alert.get("starts_at"), resolved_at),
            "time_to_detection": _duration_str(alert.get("starts_at"), completed_at),
            "date": _fmt_ts(alert.get("starts_at"), "%Y-%m-%d"),
        },
    }

    user_msg = f"Generate a postmortem for this incident:\n\n```json\n{json.dumps(context, indent=2, default=str)}\n```"

    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )

    text = response.content[0].text.strip()
    logger.info(
        "Postmortem generated: %d chars  alert=%s  service=%s",
        len(text),
        alert.get("alertname"),
        alert.get("service"),
    )
    return text
