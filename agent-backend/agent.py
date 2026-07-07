"""Claude tool-use agent: correlates an alert to the most likely bad commit."""
import json
import logging
import re

import anthropic

from tools import TOOL_DEFINITIONS, dispatch

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an automated incident response agent.

When given a production alert you must do ALL of the following in order:
1. Call get_recent_deploys for the affected service (use window_minutes=90)
2. Call get_commit_diff for every commit returned — even seemingly innocuous ones
3. Call search_runbooks with a query combining the alert type, error signature, and service name
4. Gather failure evidence appropriate to the alert type:
   - For production metric alerts (HighErrorRate, HighLatency): call query_prometheus TWICE.
     Use {window} as the rate() duration placeholder and pass `since` = the alert's starts_at:
     a. Error rate fraction: rate(http_requests_total{job="<service>",status_code=~"5.."}[{window}]) / rate(http_requests_total{job="<service>"}[{window}])
        Multiply by 100 to convert to a percentage for the impact field.
     b. Request rate (per minute): rate(http_requests_total{job="<service>"}[{window}]) * 60
     (For HighLatency alerts instead query: histogram_quantile(0.99, rate(http_request_duration_seconds_bucket{job="<service>"}[{window}])))
   - For CI/build failure alerts (CIFailure): the alert description names the branch the
     failure occurred on — pass it as the `branch` parameter to get_recent_deploys, or you
     will only see default-branch commits and miss the actual culprit.
     Call get_ci_logs instead of query_prometheus.
     Read the failed step names and log tail to extract the failure signature (test name,
     exception, build error), then correlate it against the commit diffs. Set all impact
     fields to null. Note: for CI failures, commits may legitimately be older than the
     alert — the head_sha from get_ci_logs identifies which commit actually triggered the run.
     If the logs suggest a flaky test or infrastructure issue rather than a code change,
     say so and use low confidence rather than blaming the nearest commit.
5. Output ONLY a JSON object — no prose before or after — in this exact shape:

{
  "likely_commit": {
    "sha": "<full 40-char sha or null>",
    "author": "<email>",
    "message": "<commit message>",
    "deployed_at": "<ISO timestamp>"
  },
  "confidence": "high | medium | low",
  "reasoning": "<2-3 sentences citing specific file/line changes in the diff>",
  "error_match": "<one sentence: how the diff change explains the observed error>",
  "impact": {
    "error_rate_pct": <0-100 integer from Prometheus, or null if unavailable>,
    "requests_per_min": <integer from Prometheus, or null>,
    "failed_per_min": <integer derived as requests_per_min * error_rate_pct/100, or null>,
    "p99_latency_s": <float for latency alerts, or null>
  },
  "runbook": {
    "filename": "<filename of top matching runbook>",
    "title": "<runbook title>",
    "summary": "<1-2 sentences: immediate mitigation action only — no shell commands, no multi-step procedures. Tell the on-call what to do right now.>"
  }
}

If no recent deploys exist or none look suspicious, set likely_commit to null.
Be specific — name the exact function or line that is the likely root cause.
Always include the runbook and impact fields even when likely_commit is null.
If Prometheus returns null for a metric, set the corresponding impact field to null."""


def run_agent(alert: dict) -> dict:
    """
    Run the agentic tool-use loop for a single alert.
    Returns a dict with the agent's analysis plus a tool_log for postmortem use.
    """
    client = anthropic.Anthropic()

    user_message = (
        f"Alert name   : {alert.get('alertname')}\n"
        f"Service      : {alert.get('service')}\n"
        f"Severity     : {alert.get('severity')}\n"
        f"Description  : {alert.get('description', '').strip()}\n"
        f"Fired at     : {alert.get('starts_at')}\n\n"
        "Investigate this alert and identify the most likely bad commit."
    )

    messages = [{"role": "user", "content": user_message}]
    tool_log: list[dict] = []

    for iteration in range(10):  # safety cap on tool rounds
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOL_DEFINITIONS,
            messages=messages,
        )

        logger.info("Agent round %d: stop_reason=%s", iteration + 1, response.stop_reason)
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            # Extract JSON from the first text block.
            # The model sometimes wraps the JSON in prose before a code fence,
            # so we search for the fence with regex rather than checking startswith.
            for block in response.content:
                if hasattr(block, "text"):
                    text = block.text.strip()
                    candidate = text

                    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
                    if fence:
                        candidate = fence.group(1)
                    elif not text.startswith("{"):
                        # Fall back: find the first top-level JSON object
                        brace = text.find("\n{")
                        if brace >= 0:
                            candidate = text[brace + 1:]

                    try:
                        result = json.loads(candidate)
                    except json.JSONDecodeError:
                        result = {"raw_output": block.text}
                    result["tool_log"] = tool_log
                    return result
            return {"error": "no text block in response", "tool_log": tool_log}

        if response.stop_reason != "tool_use":
            break

        # Execute tool calls, collect results
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            raw_result = dispatch(block.name, block.input)
            tool_log.append({
                "tool": block.name,
                "input": block.input,
                "result": json.loads(raw_result),
            })
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": raw_result,
            })
        messages.append({"role": "user", "content": tool_results})

    return {"error": "agent loop ended without a final answer", "tool_log": tool_log}
