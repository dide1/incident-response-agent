# Missing Retry Logic / No Circuit Breaker

## When to use
A service calling an external API or downstream service has no retry logic, no exponential backoff, and no circuit breaker. Every transient error (network blip, brief gateway unavailability) results in a failed request with no recovery attempt. Error rate is high even though the dependency is only intermittently unhealthy.

## Symptoms
- High error rate on endpoints that call external services
- Errors are intermittent rather than 100% (the dependency is flaky, not completely down)
- Error rate is much higher than the dependency's own error rate would suggest (no retries means each transient failure becomes a user-visible failure)
- Recent code review shows retry loop was removed or never added

## Why this matters
A well-behaved client retries transient errors with exponential backoff. Without retries:
- A gateway with 10% transient error rate causes 10% failure rate for end users
- With 3 retries + backoff, that same 10% rate causes ~0.1% end-user failure rate

## Diagnosis
1. Check the service code for calls to external APIs
2. Look for `try/except` blocks that catch errors but don't retry
3. Look for comments like "retries handled by SDK" — verify the SDK actually does this
4. Check recent commits for removal of a retry loop (common "cleanup" that breaks resilience)

## Immediate mitigation
- **Roll back** if a recent commit removed retry logic that previously existed
- **Enable feature flag** to route traffic around the affected code path if possible
- **Add caching** for read operations so failures don't reach the external dependency

## Fix pattern
```python
import time

def call_with_retry(fn, max_attempts=3, base_delay=0.1):
    for attempt in range(max_attempts):
        try:
            return fn()
        except TransientError as exc:
            if attempt == max_attempts - 1:
                raise
            time.sleep(base_delay * (2 ** attempt))  # exponential backoff
```

## Related
- `payment-gateway-timeout.md` — specific case: payment gateway with no retries
- `downstream-service-failure.md` — when the dependency is fully down, not just flaky
- `bad-deploy-rollback.md` — if retry logic was removed in a recent deploy
