import json
import logging

from db import fetch_commit_diff, fetch_recent_deploys, search_runbooks_db

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
    {
        "name": "search_runbooks",
        "description": (
            "Search the runbook library for the most relevant incident response guide. "
            "Provide a query combining the alert type, error signature, and service name. "
            "Returns the top 3 matching runbooks with title, content, and similarity score."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Natural language query describing the incident, e.g. "
                        "'PaymentGatewayError propagating uncaught on payments-service' or "
                        "'N+1 query causing high latency on order listing endpoint'"
                    ),
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "query_prometheus",
        "description": (
            "Run a PromQL query against Prometheus and return the current scalar value. "
            "Use this to get real-time error rates, request rates, and latency figures "
            "for the affected service so the impact field contains actual numbers. "
            "Available metrics: http_requests_total, http_request_errors_total, "
            "http_request_duration_seconds_bucket. Labels: job=<service-name>."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "promql": {
                    "type": "string",
                    "description": (
                        "PromQL expression, e.g. "
                        "'rate(http_request_errors_total{job=\"payments-service\"}[2m])' or "
                        "'histogram_quantile(0.99, rate(http_request_duration_seconds_bucket{job=\"order-service\"}[2m]))'"
                    ),
                },
            },
            "required": ["promql"],
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

    elif name == "search_runbooks":
        from embedder import embed
        query_vec = embed(inputs["query"])
        rows = search_runbooks_db(query_vec, top_k=3)
        result = rows if rows else [{"error": "No runbooks found — run /admin/ingest-runbooks first"}]

    elif name == "query_prometheus":
        from prometheus_client import query as prom_query
        value = prom_query(inputs["promql"])
        result = {"value": value, "promql": inputs["promql"]}

    else:
        result = {"error": f"Unknown tool: {name}"}

    payload = json.dumps(result, default=str)
    logger.debug("Tool result (%s): %.300s", name, payload)
    return payload
