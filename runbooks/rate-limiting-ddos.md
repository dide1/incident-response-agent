# Rate Limiting / Traffic Spike / DDoS

## When to use
Error rate or latency has spiked due to a sudden increase in inbound request volume — from legitimate traffic, a misbehaving client, a bot, or a targeted attack. The service itself is healthy; it is simply overwhelmed.

## Symptoms
- Request rate (`rate(http_requests_total[1m])`) spikes sharply
- All endpoints affected simultaneously (not a single bad endpoint)
- Errors are 429 (Too Many Requests) or 503 (Service Unavailable / queue full)
- CPU and memory spike in proportion to request volume
- Single IP or small range of IPs responsible for a disproportionate share of requests

## Diagnosis

### Confirm it is a traffic spike (not a code issue)
```promql
# Is total request volume elevated?
sum(rate(http_requests_total[2m]))
```

### Check for deploy correlation
If the spike started exactly at a deploy, it may be a load test, a retry storm from a broken client, or a bad redirect that is causing request amplification — not an external attack.

### Identify the source
- Check access logs for IP distribution
- Look for a single endpoint receiving disproportionate traffic
- Check for a missing or broken rate limit header in the API response

## Mitigation

| Timeframe | Action |
|---|---|
| Immediate (< 5 min) | Block offending IPs at load balancer / API gateway |
| Short-term | Enable rate limiting on the affected endpoints (e.g. 100 req/min per IP) |
| Short-term | Scale up replicas to handle legitimate traffic while filtering malicious |
| Long-term | Add rate limiting middleware; set request quotas per API key or user |

## Rate limit implementation (FastAPI example)
```python
from slowapi import Limiter
limiter = Limiter(key_func=get_remote_address)

@app.post("/payments/process")
@limiter.limit("60/minute")
def process_payment(request: Request, payment: dict):
    ...
```

## Related
- `downstream-service-failure.md` — if the traffic spike causes downstream overload
- `high-error-rate-investigation.md` — if error type is not clearly traffic-volume related
