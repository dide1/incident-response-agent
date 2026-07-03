# Bad Deploy Rollback

## When to use
A deployment was identified as the root cause of a production incident. Error rate or latency spiked within minutes of a deploy, and the diff review confirms the new code is responsible.

## Symptoms
- Error rate spike correlated with a specific deployment timestamp
- Alertmanager fired within 1–5 minutes of a service restart
- Metrics show a clean "step change" at the moment of the deploy

## Immediate mitigation (target: < 5 minutes)
1. **Identify the bad commit SHA** from the deploy tracker or CI/CD history
2. **Roll back the service** to the previous known-good image:
   ```bash
   docker compose up -d --no-deps --build <service>   # after reverting code
   # or in Kubernetes:
   kubectl rollout undo deployment/<service>
   ```
3. **Confirm error rate drops** in Prometheus within 60 seconds of rollback
4. **Mark the alert resolved** in Alertmanager once metrics are healthy

## Root cause investigation (after mitigation)
1. Pull the bad commit diff: `git show <sha>`
2. Reproduce locally in a staging environment
3. Add a regression test that would have caught the bug
4. Fix forward on a branch, get a code review, re-deploy with monitoring

## Escalation
If rollback does not resolve the incident, escalate — the root cause may be a data migration, schema change, or config change that can't be reverted by rolling back the binary.

## Related
- `unhandled-exception-triage.md` if the error is an uncaught exception
- `missing-retry-logic.md` if the regression removed resilience patterns
