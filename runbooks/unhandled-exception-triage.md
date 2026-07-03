# Unhandled Exception Triage

## When to use
A service is returning HTTP 500 errors caused by an exception that is not caught and handled — it propagates all the way to the framework's default error handler, which returns a bare 500 with no useful error message for the caller.

## Symptoms
- High 5xx rate on one or more endpoints
- Error logs show a repeating exception with a stack trace (KeyError, NullPointerException, TypeError, AttributeError, IndexError, etc.)
- The exception class and message are consistent across failures (same bug hitting every request)
- Errors appeared suddenly, often correlated with a deployment

## Diagnosis

### Read the stack trace
```bash
docker compose logs --tail=200 <service> | grep -A 20 "Traceback\|Exception\|Error:"
```
The stack trace tells you exactly which function and line is failing.

### Common unhandled exception patterns
| Exception | Likely cause |
|---|---|
| `KeyError: 'amount'` | `payment["amount"]` instead of `payment.get("amount")` — key not always present |
| `NullPointerException` / `AttributeError: 'NoneType'` | Calling a method on a value that can be `None` |
| `TypeError: float() argument must be ... not 'NoneType'` | `float(None)` — unvalidated input reaching type conversion |
| `IndexError: list index out of range` | Off-by-one; accessing index that doesn't exist |
| `ImportError` / `ModuleNotFoundError` | New code imports a library not installed in the deployment environment |

### Check for a recent deploy
Unhandled exceptions are almost always introduced by a code change. Query the deploy tracker for commits in the last 60–90 minutes.

## Mitigation

### Immediate
1. **Identify the exception** from logs
2. If from a recent deploy: **roll back** (`bad-deploy-rollback.md`)
3. If rollback is not possible: add a `try/except` around the failing call and return a 422 or 503 instead of letting it propagate

### Fix
- Add input validation at the API boundary (validate required fields before accessing them)
- Add a specific `except ExceptionType` clause around the failing call
- Add a test that exercises the missing-input / null-value path

## Related
- `bad-deploy-rollback.md` — roll back if exception was introduced by a recent commit
- `payment-gateway-timeout.md` — if the exception is a gateway error propagating uncaught
- `high-error-rate-investigation.md` — if the exception source is not immediately clear
