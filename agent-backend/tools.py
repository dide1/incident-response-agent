import json
import logging

from db import fetch_commit_diff, fetch_recent_deploys

logger = logging.getLogger(__name__)

# Tool schemas passed to the Claude API
TOOL_DEFINITIONS = [
    {
        "name": "get_recent_deploys",
        "description": (
            "Return all deployments to a service within the last N minutes. "
            "Each record includes: sha, author, commit_message, deployed_at, branch."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "description": "Service name, e.g. 'payments-service' or 'order-service'",
                },
                "window_minutes": {
                    "type": "integer",
                    "description": "How many minutes before now to look back (default 60)",
                    "default": 60,
                },
            },
            "required": ["service"],
        },
    },
    {
        "name": "get_commit_diff",
        "description": (
            "Fetch the git diff for a specific commit SHA. "
            "Use this to inspect exactly what code changed in a candidate commit."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sha": {
                    "type": "string",
                    "description": "Full 40-character commit SHA",
                },
            },
            "required": ["sha"],
        },
    },
]


def dispatch(name: str, inputs: dict) -> str:
    """Execute a tool call and return a JSON string result."""
    logger.info("Tool call: %s(%s)", name, json.dumps(inputs))

    if name == "get_recent_deploys":
        rows = fetch_recent_deploys(inputs["service"], inputs.get("window_minutes", 60))
        result = rows if rows else []

    elif name == "get_commit_diff":
        row = fetch_commit_diff(inputs["sha"])
        result = row if row else {"error": f"No diff found for SHA {inputs['sha']}"}

    else:
        result = {"error": f"Unknown tool: {name}"}

    payload = json.dumps(result, default=str)
    logger.debug("Tool result (%s): %s", name, payload[:300])
    return payload
