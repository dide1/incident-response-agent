# Payment Gateway Timeout / Failure

## When to use
The payments service is returning 5xx errors specifically referencing a payment gateway, payment processor, or external charging API. Errors may include `GatewayTimeoutError`, `PaymentProcessorException`, or `Connection timed out`.

## Symptoms
- High error rate concentrated on `/payments/process` or `/payments/charge`
- Error messages reference an external payment provider (Stripe, Braintree, Adyen, etc.)
- Latency on payment endpoints spikes before errors appear (gateway is slow before it fails)
- Other non-payment endpoints remain healthy

## Immediate diagnosis

### Check gateway health
Visit the payment provider's status page. Many providers publish real-time status at `status.<provider>.com`.

### Check the error message
```bash
docker compose logs --tail=200 payments-service | grep -i "gateway\|timeout\|payment"
```
- `Connection timed out`: network path to gateway is broken
- `Invalid API key`: credentials rotated or expired
- `Rate limit exceeded`: request volume too high
- `NullPointerException` / `NullReferenceError` on token: passing `None` for payment token (often guest checkout bug)

### Check whether retries are configured
If the service is calling the gateway without client-side retry logic and the gateway has transient errors, every request fails immediately. See `missing-retry-logic.md`.

## Mitigation options

| Scenario | Action |
|---|---|
| Gateway is down | Enable payment fallback / maintenance mode, alert customers |
| Credentials expired | Rotate API keys, restart service |
| Token is None for guest users | Hotfix: validate token before calling gateway; rollback if from recent deploy |
| No retry logic | Deploy fix with exponential backoff; rollback to version with retries in interim |

## Related
- `missing-retry-logic.md` — if retries were removed in a recent commit
- `bad-deploy-rollback.md` — if the gateway integration was introduced by a recent deploy
- `unhandled-exception-triage.md` — if gateway exceptions are propagating uncaught
