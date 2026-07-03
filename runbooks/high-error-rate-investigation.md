# High Error Rate Investigation

## When to use
A service is returning HTTP 5xx on more than 20% of requests and the root cause is not immediately obvious from the alert description.

## Symptoms
- `http_request_errors_total` rate exceeds threshold in Prometheus
- Error rate is sustained (not a brief spike that self-resolved)
- Multiple endpoints or a single critical endpoint affected

## Diagnosis steps

### 1. Identify the scope
```promql
# Which endpoints are failing?
sum by (endpoint) (rate(http_request_errors_total[2m]))

# What percentage?
sum by (job) (rate(http_request_errors_total[2m]))
/ sum by (job) (rate(http_requests_total[2m]))
```

### 2. Check for a recent deploy
Query the deploy tracker for commits in the last 60–90 minutes. If a deploy exists, treat this as a bad-deploy incident and follow `bad-deploy-rollback.md`.

### 3. Read the error logs
```bash
docker compose logs --tail=100 <service> | grep -i "error\|exception\|traceback"
```
Look for a repeating exception class or message. The stack trace will tell you exactly which function is failing.

### 4. Check downstream dependencies
If the service calls another service or external API, check whether those dependencies are healthy. A downstream failure will surface as 5xx in the calling service even when the calling service's own code is fine.

### 5. Check resource constraints
- CPU throttling, memory pressure, or connection pool exhaustion can cause request timeouts that surface as 5xx errors.

## Mitigation
Depends on root cause:
- Bad deploy → `bad-deploy-rollback.md`
- Unhandled exception → `unhandled-exception-triage.md`
- Downstream failure → `downstream-service-failure.md`
- Resource exhaustion → `db-connection-pool-exhaustion.md`
