# Service Memory Leak

## When to use
A service's memory usage grows continuously over time and does not stabilize. Eventually the container hits its memory limit and is OOM-killed (restarting with a brief outage), or performance degrades as the OS starts swapping.

## Symptoms
- Container memory metric grows steadily over hours or days
- Service restarts unexpectedly (OOM kill in `docker inspect` or Kubernetes events)
- Latency increases gradually over time and resets after service restart (memory pressure causes GC pauses)
- Heap dumps show a growing number of objects that are never collected

## Diagnosis

### Confirm memory is growing
```bash
docker stats <container> --no-stream
```
Check `MEM USAGE / LIMIT`. Run periodically to confirm growth is monotonic.

### Identify what's leaking (Python)
```python
import tracemalloc
tracemalloc.start()
# ... run for a while ...
snapshot = tracemalloc.take_snapshot()
top_stats = snapshot.statistics("lineno")
for stat in top_stats[:10]:
    print(stat)
```

### Common leak patterns
| Pattern | Description |
|---|---|
| Growing list/dict in module scope | Cache or buffer that is never evicted |
| Event listeners not removed | Callbacks registered but never unregistered |
| Circular references | Python's GC handles most, but `__del__` can prevent collection |
| Unclosed file / DB connections | Resources not released in `finally` block |
| Long-lived request contexts | Per-request data stored in global state |

## Mitigation

### Immediate
- **Restart the service** to restore memory to baseline (buys time, does not fix the leak)
- Set a **memory limit** on the container so OOM kills are fast and predictable, not slow degradation

### Fix
1. Profile with `tracemalloc` or `memory_profiler` to find the growing object type
2. Add eviction/TTL to any in-memory cache
3. Ensure all context managers are used (`with` blocks) for files, connections, and locks
4. Add a regression test: run the endpoint N times and assert memory growth is bounded

## Related
- `api-latency-spike.md` — memory pressure causes GC pauses that look like latency spikes
- `db-connection-pool-exhaustion.md` — connection leaks are a form of resource leak
