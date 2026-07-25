# Troubleshooting

Symptoms, likely causes, and the command that confirms it. For routine
procedures see [operations.md](operations.md).

## Start here

```powershell
docker compose ps                  # what is actually running
Invoke-RestMethod http://127.0.0.1:8000/health | ConvertTo-Json -Depth 4
docker compose logs --tail 50 api
```

`/health` reports `database` and `redis` separately. `degraded` means one
dependency is down but the gateway is still serving; `unhealthy` means both
are unreachable.

Every response carries an `X-Correlation-ID`. It appears in the structured
logs, the request log row, and any audit event for that request, so it is the
fastest way to trace one failure end to end.

## Startup

### `TOKEN_GUARD_MASTER_KEY is not set`

The application refuses to start without an encryption key, because starting
without one would mean storing provider credentials in the clear.

```powershell
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Put the value in `.env` as `TOKEN_GUARD_MASTER_KEY`.

### `InvalidToken` when reading a credential

The master key does not match the one used to encrypt the stored credential.
Either the key was rotated or regenerated, or the database came from another
environment. Restore the original key; there is no way to recover the
credentials without it. If it is genuinely lost, delete and re-enter the
affected credentials.

### JWT secret rejected as too short

`JWT_SECRET_KEY` must be long enough to be meaningful for HS256:

```powershell
uv run python -c "import secrets; print(secrets.token_urlsafe(64))"
```

Changing it invalidates every issued access token, forcing all clients to log
in again.

## Database

### Connection refused

```powershell
docker compose ps postgres
docker compose logs --tail 30 postgres
```

Note the port mapping: the development stack publishes PostgreSQL on **5433**,
not 5432, to avoid colliding with a local install. `DATABASE_URL` must match.
The production overlay publishes no port at all — connect through
`docker compose exec`.

### `relation "..." does not exist`

Migrations have not been applied to this database.

```powershell
uv run alembic current
uv run alembic upgrade head
```

### `Can't locate revision identified by ...`

The database records a revision that is not in `alembic/versions/`, usually
after switching branches. Check out the branch that contains that migration
and downgrade before switching back, or recreate the database if it holds no
data worth keeping.

### `alembic check` reports pending operations

A model changed with no matching migration. Generate one and review it before
committing — autogenerate misses things like server defaults and index renames:

```powershell
uv run alembic revision --autogenerate -m "describe the change"
```

### Multiple heads

```powershell
uv run alembic heads
uv run alembic merge -m "merge heads" <revision-a> <revision-b>
```

## Redis

### Connection refused

```powershell
docker compose ps redis
docker compose exec redis redis-cli ping
```

In production Redis requires a password, so add `-a $env:REDIS_PASSWORD`. If
`REDIS_URL` is missing the password, every operation fails with `NOAUTH`.

The gateway degrades rather than failing when Redis is down: exact caching
stops, and rate limiting falls back to database-backed counting, which is
coarser. `/health` reports `degraded`.

### Cache hit rate suddenly dropped to zero

Expected after a Redis restart without persistence — the cache is
deliberately not authoritative. It refills as traffic arrives. If it stays at
zero, check `USE_REDIS_EXACT_CACHE` and that `use_cache` is not being set to
false by the client.

## Providers

### Ollama unavailable

```powershell
ollama list
curl.exe http://localhost:11434/api/tags
```

From inside a container, `localhost` is the container itself. Use
`http://host.docker.internal:11434`, which is what the Compose files set.

### OpenRouter 401

The stored credential is wrong or revoked. Test it without sending a chat
request:

```powershell
Invoke-RestMethod -Uri "$baseUrl/v1/credentials/1/test" -Method Post -Headers $projectHeaders
```

Then rotate it: `POST /v1/credentials/1/rotate` keeps the same credential ID,
so nothing referencing that ID needs to change.

### OpenRouter out of credits

Expected behaviour is a fallback to Ollama when policy allows it. Confirm from
the response: `reliability.fallback_used` is true and
`routing.fallback.attempts_made` is above 1. If it did not fall back, check
`config/routing_policy.json` and that an Ollama route is enabled.

### 503 with every provider attempt failed

All configured providers failed for this request. `reliability.provider_attempts`
shows how many were tried. Check circuit breaker state:

```promql
tokenguard_circuit_breaker_state
```

A value of 1 means open — requests are rejected without an attempt. It
half-opens automatically after `CIRCUIT_BREAKER_RESET_TIMEOUT_SECONDS`. A
breaker that keeps reopening means the provider is genuinely unhealthy.

### Requests are slow but succeed

Compare `latency_ms` against `tokenguard_provider_latency_seconds`. If most of
the time is provider-side, the gateway is not the bottleneck; a large local
model on CPU is routinely slow. Gateway overhead itself is covered in
[performance.md](performance.md).

## Authentication

### Project key rejected (401)

Check, in order:

- the header is `X-TokenGuard-Key`, not `Authorization`
- no `Bearer ` prefix was added
- the whole key was sent, including the `tg_live_` prefix
- the key is active and not expired
- the project is active

Only the hash is stored, so a truncated key cannot be recovered or matched.

### Access token rejected (401)

Expired (15 minutes by default), signed with a different `JWT_SECRET_KEY`, or
carrying the wrong issuer or audience. Refresh it. Also confirm the user is
still active — deactivating a user invalidates their tokens immediately.

### Refresh token rejected after it worked once

Working as designed. Refresh tokens rotate on use, and presenting one twice
revokes the whole chain as a stolen-token response. Store only the newest
refresh token and never share one across processes.

### 403 on an organization endpoint

The user is authenticated but lacks the permission, or is not a member of that
organization. Check `GET /v1/organizations/{id}/members`. Note that a
*non-member* gets 404 rather than 403, so IDs cannot be enumerated.

### 404 for something that definitely exists

Almost always tenant isolation. The resource belongs to another project or
organization, and 404 is returned deliberately in place of 403. Verify you are
using the key for the owning project.

## Governance and limits

### Legitimate prompt blocked as injection

Check `governance.prompt_injection_findings` for the category and matched
pattern. Detection is pattern-based, so phrases like "ignore the previous
instructions" in a quoted document can trigger it. Raise
`PROMPT_INJECTION_BLOCK_THRESHOLD`, or set a per-project policy through
`/v1/projects/{id}/governance-policy`.

### Content silently rewritten

Not silent — `governance.sanitized` is true and `sensitive_data_findings`
lists the entity types (never the values). PII redaction replaced them with
`<REDACTED>`. Set `PII_DEFAULT_ACTION=block` to reject instead of rewriting.

### 429 responses

Either the per-project rate limit or the daily budget. `budget.reasons`
distinguishes them. Honour `Retry-After`. Budgets reset daily at UTC midnight;
change the limits on the project rather than disabling enforcement.

### Response never returned, marked as blocked

Output secret scanning found a credential-shaped string in the model response.
It is deliberately neither returned nor cached. The audit event records the
category only, not the content. Investigate the prompt: a leaking system
prompt can cause this.

## Workers

### Job stays `queued`

No Celery worker is consuming the queue.

```powershell
docker compose ps worker
docker compose logs --tail 50 worker
docker compose exec redis redis-cli -n 2 llen celery
```

Jobs submitted while the worker is down are not lost; they run once it
returns.

### Scheduled tasks never run

Celery Beat is a separate process from the worker. Both must be running:

```powershell
docker compose ps beat
```

### Worker was killed mid-job

`task_acks_late` means the task is redelivered rather than lost. Tasks are
written to be idempotent, so a retry does not double-count aggregates. A job
left `running` with no active worker is stale — check `docker compose ps` before
concluding it is stuck.

## Observability

### `/metrics` returns 404

`ENABLE_PROMETHEUS_METRICS` is false.

### Prometheus shows the target as down

From inside the Prometheus container the API is `api:8000`, not `localhost`.
Check `observability/prometheus.yml` and that both containers share a network.

### Prometheus will not start after a config change

Usually a bad rule file. Validate before restarting:

```powershell
docker run --rm -v "${PWD}/observability:/rules" `
    --entrypoint promtool prom/prometheus:latest `
    check rules /rules/alert_rules.yml
```

### No traces in Jaeger

`ENABLE_OPENTELEMETRY` must be true and the OTLP endpoint reachable. Note the
port split in the development stack: the collector is published on **4319**
because Jaeger already holds 4318.

## Tests

### `Tests must run against tokenguard_test`

A guard in `tests/conftest.py`, preventing a test run from truncating a real
database. Point `DATABASE_URL` or `TEST_DATABASE_URL` at `tokenguard_test`.

### Tests fail only when run in parallel

They share a database and Redis. `-n auto` needs per-worker isolation, which is
not configured. Run the integration and end-to-end suites serially.

### Coverage gate fails at just under 85%

The threshold is enforced in `pyproject.toml` and CI. Find the gap:

```powershell
uv run pytest --cov=app --cov-branch --cov-report=html
```

Then open `htmlcov/index.html`.
