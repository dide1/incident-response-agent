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
                "branch": {
                    "type": "string",
                    "description": (
                        "Git branch to inspect (GitHub-backed services only). "
                        "IMPORTANT for CI failures: the failure happens on a specific "
                        "branch — pass it, or you will only see default-branch commits "
                        "and miss the actual culprit."
                    ),
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
        "name": "get_ci_logs",
        "description": (
            "Fetch the failed-job logs for a GitHub Actions CI run. Use this for "
            "CI/build failure alerts instead of query_prometheus. Returns the "
            "workflow name, head commit sha, branch, failed job names, failed step "
            "names, and per-job log output. Each job entry contains either "
            "'log_analysis' (structured: failed_tests list, error_signatures list, "
            "stack_traces list) when the analyzer is available, or 'log_tail' "
            "(raw last 150 lines). Prefer log_analysis fields when present — they "
            "are pre-parsed and deduplicated for faster diagnosis."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "description": "Service/repo name from the alert, e.g. 'loupe'",
                },
                "run_id": {
                    "type": "integer",
                    "description": (
                        "GitHub Actions run ID if known (from the alert description). "
                        "Omit to use the most recent failed run."
                    ),
                },
            },
            "required": ["service"],
        },
    },
    {
        "name": "query_prometheus",
        "description": (
            "Run a PromQL query against Prometheus and return the current scalar value. "
            "Use this to get real-time error rates, request rates, and latency figures "
            "for the affected service so the impact field contains actual numbers. "
            "Available metrics: http_requests_total (with status_code label for errors — "
            "filter with status_code=~\"5..\" for 5xx), "
            "http_request_duration_seconds_bucket. Labels: job=<service-name>. "
            "IMPORTANT: use {window} as the rate() duration placeholder — it will be "
            "replaced server-side with the exact elapsed incident duration so the window "
            "covers the spike without diluting with pre-incident baseline traffic. "
            "Pass `since` as the alert's starts_at ISO timestamp."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "promql": {
                    "type": "string",
                    "description": (
                        "PromQL expression using {window} as the duration placeholder, e.g. "
                        "'rate(http_requests_total{job=\"payments-service\",status_code=~\"5..\"}[{window}]) "
                        "/ rate(http_requests_total{job=\"payments-service\"}[{window}])' "
                        "or 'histogram_quantile(0.99, rate(http_request_duration_seconds_bucket"
                        "{job=\"order-service\"}[{window}]))'"
                    ),
                },
                "since": {
                    "type": "string",
                    "description": (
                        "ISO 8601 timestamp of when the alert fired (the starts_at field). "
                        "Used to compute the {window} duration to match incident elapsed time "
                        "and avoid diluting impact numbers with pre-incident normal traffic."
                    ),
                },
            },
            "required": ["promql"],
        },
    },
]


def dispatch(name: str, inputs: dict, alert_context: dict | None = None) -> str:
    """Execute a tool call and return a JSON string result."""
    logger.info("Tool call: %s(%s)", name, json.dumps(inputs))

    ctx = alert_context or {}
    github_token: str | None = ctx.get("github_token")
    github_repo: str | None = ctx.get("github_repo")

    if name == "get_recent_deploys":
        from github_client import list_recent_commits, repo_for_service
        service = inputs["service"]
        window = inputs.get("window_minutes", 60)
        if github_repo or repo_for_service(service):
            rows = list_recent_commits(
                service, window,
                branch=inputs.get("branch"),
                github_repo=github_repo,
                token=github_token,
            )
        else:
            rows = fetch_recent_deploys(service, window)
        result = rows if rows else []

    elif name == "get_commit_diff":
        row = fetch_commit_diff(inputs["sha"])
        if row is None:
            from github_client import fetch_commit_diff as gh_fetch_diff
            row = gh_fetch_diff(inputs["sha"], token=github_token)
        result = row if row else {"error": f"No diff found for SHA {inputs['sha']}"}

    elif name == "get_ci_logs":
        from github_client import fetch_ci_logs
        result = fetch_ci_logs(
            inputs["service"], inputs.get("run_id"),
            github_repo=github_repo, token=github_token,
        )

    elif name == "search_runbooks":
        from embedder import embed
        query_vec = embed(inputs["query"])
        rows = search_runbooks_db(query_vec, top_k=3)
        result = rows if rows else [{"error": "No runbooks found — run /admin/ingest-runbooks first"}]

    elif name == "query_prometheus":
        from prometheus_client import query as prom_query
        value = prom_query(inputs["promql"], since=inputs.get("since"))
        result = {"value": value, "promql": inputs["promql"], "since": inputs.get("since")}

    else:
        result = {"error": f"Unknown tool: {name}"}

    payload = json.dumps(result, default=str)
    logger.debug("Tool result (%s): %.300s", name, payload)
    return payload
