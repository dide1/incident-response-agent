# Downstream Service Failure / Cascade

## When to use
A service is returning errors not because of its own code, but because a dependency it calls (another microservice, an external API, or a third-party provider) is unavailable or slow. The error originates downstream and propagates up.

## Symptoms
- 5xx errors in service A, but service A's own code and DB are healthy
- Error messages reference another service: "order-service unavailable," "upstream timeout," "connection refused"
- The downstream service's own health check or Prometheus metrics show it is also failing
- Error rate in the calling service tracks the error rate in the called service

## Diagnosis

### Confirm the upstream service is actually failing
```bash
curl -s http://<downstream-service>/health
docker compose logs --tail=50 <downstream-service>
```

### Check whether the caller has a circuit breaker or fallback
If there is no circuit breaker, every request to the caller generates a request to the failing downstream service, amplifying the load on an already-struggling dependency.

### Check timeout configuration
If the caller's timeout is too long (e.g. 30s) and the downstream is slow, requests will queue up and connections will be held, potentially exhausting the connection pool.

## Mitigation

| Priority | Action |
|---|---|
| 1 | Fix or restart the downstream service |
| 2 | If fix is not immediate, enable a fallback or degraded mode in the caller |
| 3 | Temporarily short-circuit the dependency: return cached data or an empty response |
| 4 | Reduce timeout in the caller to fail fast and free up connections |

## Architectural improvements (post-incident)
- Add a circuit breaker (e.g. Hystrix, resilience4j, or a simple in-process counter)
- Cache read-only responses with a short TTL so the caller can serve stale data during outages
- Return a graceful degraded response instead of propagating the 5xx

## Related
- `bad-deploy-rollback.md` — if the downstream failure was triggered by a bad deploy to that service
- `missing-retry-logic.md` — if the caller is not retrying transient errors
