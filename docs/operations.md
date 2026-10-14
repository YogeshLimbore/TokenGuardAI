# Operations

Day-2 operations for TokenGuard AI. For diagnosing a specific error, see
[troubleshooting.md](troubleshooting.md).

## Service control

The development stack takes no `-f` flags. Production needs both, plus the
env file:

```powershell
$prod = "-f", "docker-compose.yml", "-f", "docker-compose.prod.yml",
        "--env-file", ".env.production"
```

```powershell
docker compose ps                       # what is running
docker compose logs -f api              # follow one service
docker compose logs --tail 100 worker
docker compose restart api
docker compose up -d --scale worker=3   # more workers; keep beat at one
docker compose down                     # stop, keeping volumes
```

`docker compose down -v` deletes the volumes, and therefore the database. It is
never part of a routine restart.

## Migrations

```powershell
uv run alembic current      # applied revision
uv run alembic heads        # expected revision(s)
uv run alembic history
uv run alembic upgrade head
uv run alembic check        # fails when a model change has no migration
```

The API container also runs `alembic upgrade head` at start. For a controlled
rollout, migrate as a separate step first, as `deploy_production.ps1` does.

## Health

```bash
curl -s http://localhost:8000/live
curl --fail -s http://localhost:8000/ready
curl -s http://localhost:8000/health
```

`/live` confirms the process is running. `/ready` returns HTTP 503 until both
PostgreSQL and Redis are usable and is the endpoint used by Docker Compose.
`/health` retains the detailed dependency report for operators.

## Metrics

Prometheus scrapes `/metrics` (see `observability/prometheus.yml`).

Useful series:

- HTTP request latency and status counts
- Provider attempt / fallback counters
- Cache hit ratios
- Governance / security event counters (when enabled)

## Traces

With the Compose observability profile, Jaeger UI listens on
`http://localhost:16686`. The API exports OTLP to the collector.

## Logs

Logs are JSON by default (`LOG_FORMAT=json`). Every request carries an
`X-Correlation-ID` (accepted from the client or generated).

Never expect passwords, JWTs, refresh tokens, or provider secrets in logs —
those are intentionally excluded.

## Common failures

| Symptom | Likely cause | Action |
|---------|--------------|--------|
| API crash-loops on start | Missing/invalid production secrets | Check `JWT_SECRET_KEY`, `TOKEN_GUARD_MASTER_KEY`, `DEBUG` |
| `alembic` revision missing | Stale image vs DB | Rebuild API image; run `./scripts/migrate.sh` |
| Chat returns 401 | Missing/invalid project API key | Create key via org/admin project routes |
| Login returns 429 | Auth rate limit | Wait for window; check Redis |
| Login returns "temporarily locked" | Account lockout | Wait `LOCK_MINUTES` (15) or unlock via DB for emergencies |
| Redis warnings at startup | Redis down | Caching/rate limits degrade to DB fallbacks |
| Docs 404 in production | Expected | `/docs` disabled when `ENVIRONMENT=production` |

## Backups

PostgreSQL is the only system of record. Redis holds cache entries, rate-limit
counters, and circuit-breaker state; it may be flushed or rebuilt at any time
and is never the authoritative source for credentials, projects, users, audit
history, or billing data. Redis AOF is enabled in Compose only to shorten
warm-up after a restart.

Create a backup:

```powershell
.\scripts\backup_postgres.ps1
.\scripts\backup_postgres.ps1 -Database tokenguard -OutputDirectory D:\backups
```

The script runs `pg_dump -Fc` inside the container and copies the dump out with
`docker cp`, so binary data is never piped through PowerShell.

`TOKEN_GUARD_MASTER_KEY` is deliberately **not** in the dump. Provider
credentials inside it stay Fernet-encrypted, so the key must be backed up
separately in a secrets manager. Losing it means restored provider credentials
cannot be decrypted — the rest of the database still restores fine. Encrypt the
backup artifacts themselves too: they contain hashed credentials and audit data.

### Restore validation

A backup is not proven until a restore succeeds:

```powershell
.\scripts\restore_test.ps1 -DumpPath backups\tokenguard-20260725-120000.dump
```

The script restores into a scratch database (`tokenguard_restore_test`), prints
row counts for the core tables and the Alembic revision, then fails if any
provider credential looks like plaintext, any project key hash is too short to
be a hash, or any refresh token is not a 64-character digest. Add
`-KeepDatabase` to inspect the result manually.

Run it on a schedule, not only after an incident.

## Redis maintenance

Redis holds only cache entries, rate-limit counters, and circuit-breaker
state, so it can be flushed at any time. The cost is a cold cache, not data
loss.

```powershell
docker compose exec redis redis-cli ping
docker compose exec redis redis-cli info keyspace
docker compose exec redis redis-cli --scan --pattern "tokenguard:exact-cache:*" | Measure-Object

# Clear one cache namespace rather than the whole database
docker compose exec redis redis-cli --scan --pattern "tokenguard:exact-cache:*" |
    ForEach-Object { docker compose exec -T redis redis-cli del $_ }
```

In production Redis requires a password, so add `-a $env:REDIS_PASSWORD` to
each `redis-cli` call.

Never use `FLUSHALL` in production without checking which database is which:
the broker (2) and result backend (3) hold in-flight Celery work, so flushing
them loses queued jobs.

## Celery queues

```powershell
docker compose exec redis redis-cli -n 2 llen celery   # queue depth
docker compose logs --tail 100 worker
docker compose restart worker
docker compose ps beat                                 # exactly one instance
```

A queue depth that grows and never drains means workers are down, crash
looping, or saturated. Jobs submitted while workers are down are not lost —
they run once a worker returns.

Restarting a worker mid-task is safe: `task_acks_late` causes redelivery, and
tasks are idempotent. Verify aggregates afterwards.

## Investigating a failed provider request

Start from the correlation ID, which appears in the response, the logs, and the
audit trail.

```sql
-- what happened for one request
SELECT provider, model, status, latency_ms, error_message, created_at
FROM llm_request_logs
WHERE request_id = '<request-id>';

-- every attempt, including the ones that failed before fallback
SELECT provider, model, attempt_number, result, error_type, created_at
FROM provider_attempt_logs
WHERE request_id = '<request-id>'
ORDER BY attempt_number;

-- error rate by provider over the last hour
SELECT provider, status, COUNT(*)
FROM llm_request_logs
WHERE created_at > now() - interval '1 hour'
GROUP BY provider, status
ORDER BY provider;
```

```powershell
docker compose exec postgres psql -U tokenguard -d tokenguard
```

## Investigating circuit breakers

```promql
tokenguard_circuit_breaker_state          # 0 closed, 0.5 half-open, 1 open
tokenguard_provider_health_score          # below 40 means routing avoids it
```

```sql
SELECT provider, event_type, created_at
FROM provider_health_events
ORDER BY created_at DESC
LIMIT 20;
```

An open breaker rejects requests without attempting them, and half-opens after
`CIRCUIT_BREAKER_RESET_TIMEOUT_SECONDS`. Repeated reopening means the provider
is genuinely unhealthy — check credentials and quota before raising the
threshold. Restarting the API resets breaker state, which is a blunt
instrument, not a fix.

## Reviewing security events

```powershell
Invoke-RestMethod `
    -Uri "http://127.0.0.1:8000/v1/security/events?limit=50" `
    -Headers $projectHeaders
```

```sql
SELECT event_type, severity, action, COUNT(*)
FROM security_audit_events
WHERE created_at > now() - interval '24 hours'
GROUP BY event_type, severity, action
ORDER BY COUNT(*) DESC;
```

Raw prompt and response content is not stored while
`STORE_RAW_SECURITY_CONTENT=false`, so these rows carry categories and a
sanitized summary only. That is deliberate: the audit trail must not itself
become a PII store.

Escalate promptly on `output_secret_detected` — a model response contained
credential-shaped content and was withheld.

## Rotating credentials and secrets

### Provider credentials

Rotation keeps the credential ID, so nothing referencing it needs updating:

```powershell
Invoke-RestMethod -Uri "$baseUrl/v1/credentials/1/rotate" `
    -Method Post -Headers $projectHeaders `
    -ContentType "application/json" `
    -Body (@{ api_key = "sk-new-value" } | ConvertTo-Json)
```

### Project API keys

Create the replacement first, migrate clients, then deactivate the old key.
Keys are hashed, so a lost key cannot be recovered — only replaced.

## Rotating secrets

1. Generate a new secret.
2. Deploy with dual-read only if the secret type supports it (most do not —
   schedule a short maintenance window).
3. For `TOKEN_GUARD_MASTER_KEY`, existing Fernet ciphertexts become unreadable —
   plan a re-encryption migration before rotating.
4. For `JWT_SECRET_KEY`, all access tokens invalidate immediately; users must
   re-authenticate via refresh/login (refresh tokens are independent of JWT
   signing but access tokens will fail until refreshed).
5. Revoke compromised project/admin keys immediately.

## Failure drills

Run these against a development or staging stack before every release. The
automated equivalents live in `tests/integration/test_failure_degradation.py`;
the manual drills additionally prove the container topology behaves.

### Redis outage

```powershell
docker compose stop redis
# exercise /health, login, and a chat request
docker compose start redis
```

Expect: `/health` reports `degraded`, rate limiting falls back to the database,
chat still succeeds when it is otherwise safe, exact caching becomes
unavailable, and no Redis connection string appears in any response.

### PostgreSQL outage

```powershell
docker compose stop postgres
# exercise /health and an authenticated request
docker compose start postgres
```

Expect: `/health` reports the database unhealthy, authenticated requests fail
with 503, and no stack trace or DSN is returned to the caller.

### Provider outage

Point a provider at a dead port (for example `OPENROUTER_BASE_URL` at
`http://127.0.0.1:9999`) and restart the API. Expect: the retry policy runs, the
circuit breaker records failures, traffic falls back to Ollama where policy
permits, the attempt history is preserved on the response, no credential is
logged, and a total outage returns 503 with nothing written to the cache.
Restore the URL afterwards.

### Celery worker outage

```powershell
docker compose stop worker
# create a background job -> it must stay "queued"
docker compose start worker
# the job must then execute
```

Because `task_acks_late` is enabled, a worker killed mid-task re-runs it. Tasks
are written to be idempotent, so recovery must not create duplicate aggregate
records — verify the aggregates after the drill.

## Demo data

For a walkthrough or a screenshot, seed synthetic data rather than using real
traffic. The script refuses to run when `ENVIRONMENT=production`, because the
demo password is printed to the console.

```powershell
uv run python scripts/seed_demo.py
uv run python scripts/seed_demo.py -- --reset   # delete and recreate
```

It creates a demo user, organization, project, a week of plausible request
history, sample routing records, and sample security events. It never creates a
provider credential, so no real key is involved and no provider is called. The
generated project API key is printed once.

## Incident response

1. Capture `X-Correlation-ID` from the client or logs.
2. Query security / identity audit tables for the window.
3. Revoke refresh tokens / API keys as needed.
4. Rotate affected secrets.
5. File a post-incident note and update runbooks.
