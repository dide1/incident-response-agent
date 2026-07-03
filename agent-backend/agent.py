"""Claude tool-use agent: correlates an alert to the most likely bad commit."""
import json
import logging

import anthropic

from tools import TOOL_DEFINITIONS, dispatch

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an automated incident response agent.

When given a production alert you must:
1. Call get_recent_deploys for the affected service (use window_minutes=90)
2. Call get_commit_diff for every commit returned — even seemingly innocuous ones
3. Identify which commit most likely caused the alert by matching:
   - The error type / exception message in the alert description
   - Code changes in the diff (missing try/except, N+1 queries, external calls, etc.)
4. Output ONLY a JSON object — no prose before or after — in this exact shape:

{
  "likely_commit": {
    "sha": "<full 40-char sha or null>",
    "author": "<email>",
    "message": "<commit message>",
    "deployed_at": "<ISO timestamp>"
  },
  "confidence": "high | medium | low",
  "reasoning": "<2-3 sentences citing specific file/line changes in the diff>",
  "error_match": "<one sentence: how the diff change explains the observed error>"
}

If no recent deploys exist or none look suspicious, set likely_commit to null.
Be specific — name the exact function or line that is the likely root cause."""


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
            # Extract JSON from the first text block
            for block in response.content:
                if hasattr(block, "text"):
                    text = block.text.strip()
                    # Strip markdown code fences if present
                    if text.startswith("```"):
                        text = "\n".join(text.split("\n")[1:])
                        text = text.rstrip("`").strip()
                    try:
                        result = json.loads(text)
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
