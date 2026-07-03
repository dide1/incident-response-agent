# API Latency Spike

## When to use
P99 (or P95) API latency has increased significantly — typically 2–10× the normal baseline — without a corresponding increase in error rate. Requests are slow but still succeeding.

## Symptoms
- `histogram_quantile(0.99, ...)` alert fires
- Latency increased at a specific point in time (correlated with deploy, traffic spike, or external event)
- Error rate is not elevated (requests succeed, just slowly)
- Users may report timeouts if downstream callers have strict SLAs

## Diagnosis

### Identify which endpoints are slow
```promql
histogram_quantile(0.99, sum by (endpoint) (rate(http_request_duration_seconds_bucket[5m])))
```

### Correlate with a deploy
Check the deploy tracker for commits in the last 60–90 minutes. Latency spikes are commonly caused by:
- N+1 query introduced in a code change (see `n-plus-one-query.md`)
- A synchronous external API call added inline (previously async or cached)
- Removed connection pooling or caching

### Check the database
```sql
SELECT query, mean_exec_time, calls
FROM pg_stat_statements
ORDER BY mean_exec_time DESC
LIMIT 10;
```
A slow query is the most common root cause of API latency spikes.

### Check for resource saturation
- CPU throttling: container hitting its CPU limit causes latency without errors
- Memory pressure: GC pauses from high memory usage add latency
- Connection pool queue: requests waiting for a DB connection before they can even start

### Check downstream services
If the slow endpoint calls another service, check that service's latency metrics. Latency cascades upstream.

## Mitigation
| Root cause | Action |
|---|---|
| N+1 query | See `n-plus-one-query.md` |
| Slow external API call | Add timeout + async handling or caching |
| DB slow query | Add index; optimize query; rollback if regression |
| Resource saturation | Scale up (add replicas or increase resource limits) |
| Recent deploy | Rollback (`bad-deploy-rollback.md`) |
