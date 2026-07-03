# Database Connection Pool Exhaustion

## When to use
The service is returning errors that reference database connectivity — "connection pool exhausted," "too many connections," "could not obtain connection," or similar. Requests queue up waiting for a DB connection and eventually time out.

## Symptoms
- 5xx errors referencing database connection or pool
- Request latency spikes before error rate climbs (requests are waiting in pool queue)
- Postgres `pg_stat_activity` shows connections at or near `max_connections`
- Errors occur under load but not at low traffic (pool is sufficient at low concurrency)

## Diagnosis

### Check current connection count (Postgres)
```sql
SELECT count(*), state FROM pg_stat_activity GROUP BY state;
-- 'idle' connections are wasted; 'active' are doing work; 'idle in transaction' is a red flag
```

### Check your pool configuration
```bash
# Typical pool config (SQLAlchemy example)
pool_size=10, max_overflow=20, pool_timeout=30
```
If `pool_size + max_overflow < peak_concurrency * connections_per_request`, the pool will exhaust.

### Check for idle-in-transaction connections
Long-running transactions hold connections even when idle. Look for:
```sql
SELECT pid, now() - pg_stat_activity.query_start AS duration, query, state
FROM pg_stat_activity
WHERE state = 'idle in transaction' AND duration > interval '30 seconds';
```

## Mitigation

| Severity | Action |
|---|---|
| Immediate | Kill idle-in-transaction connections: `SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE state = 'idle in transaction'` |
| Short-term | Increase pool size or add a read replica |
| Long-term | Fix connection leaks, add connection timeouts, use PgBouncer for pooling |

## Common causes
- A slow query holding a connection open for seconds
- A missing `finally` block that leaks connections on exception
- A code change that opens connections without closing them (context manager removed)

## Related
- `n-plus-one-query.md` — slow queries can cause connection exhaustion indirectly
- `high-error-rate-investigation.md` — for general 5xx investigation
