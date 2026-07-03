# N+1 Query Performance Incident

## When to use
API latency has spiked and profiling or code review reveals the service is issuing one database query per item in a list rather than fetching all items in a single query. P99 latency scales linearly with the number of records.

## Symptoms
- P99 latency spike (e.g. from 50ms to 2–5s) with no change in traffic volume
- Latency grows proportionally as data volume grows
- Database slow-query logs show many near-identical `SELECT` statements in rapid succession
- Alert: `HighLatency` on a listing or collection endpoint

## How to confirm
```promql
# Is latency on listing endpoints specifically high?
histogram_quantile(0.99, rate(http_request_duration_seconds_bucket{endpoint="/orders"}[2m]))
```

```bash
# Count similar queries in DB logs (Postgres)
grep "SELECT" /var/log/postgresql/postgresql.log | sort | uniq -c | sort -rn | head -20
```

Look for a pattern like `SELECT * FROM customers WHERE id = $1` appearing hundreds of times per second.

## Root cause patterns
1. **ORM lazy loading**: fetching related records outside a transaction triggers one query per relation
2. **Loop with per-item fetch**: `for item in items: db.fetch(item.id)` instead of `db.fetch_all(ids)`
3. **Missing JOIN**: replaced a bulk query with individual lookups after a refactor

## Mitigation

### Immediate (reduce impact)
- **Add caching** in front of the affected endpoint (Redis, in-memory LRU) for idempotent reads
- **Rate-limit** the endpoint to reduce DB pressure while a fix is prepared
- **Rollback** if the N+1 was introduced by a recent deploy

### Fix (in code)
Replace per-item queries with a single bulk query:
```python
# Before (N+1)
for order in orders:
    order["customer"] = db.fetch_one("SELECT * FROM customers WHERE id = %s", order["customer_id"])

# After (1 query)
ids = [o["customer_id"] for o in orders]
customers = {c["id"]: c for c in db.fetch_all("SELECT * FROM customers WHERE id = ANY(%s)", ids)}
for order in orders:
    order["customer"] = customers[order["customer_id"]]
```

## Related
- `api-latency-spike.md` — for latency incidents not caused by N+1
- `bad-deploy-rollback.md` — if the N+1 was introduced by a recent commit
