# Architecture

TokenGuard AI is an LLM gateway. Client applications call TokenGuard instead of
provider APIs directly. TokenGuard authenticates the caller, enforces budgets
and governance, routes/falls back across providers, caches responses, and
records usage + security events.

## High-level request flow

```text
Client
  |
  |  X-TokenGuard-Key  (machine / chat)
  |  or Bearer JWT     (human / admin)
  v
FastAPI (app/main.py)
  |
  +-- Middleware
  |     Correlation ID
  |     Prometheus metrics
  |     Security headers
  |     CORS / Trusted Host (config)
  |
  +-- AuthN
  |     Project API key  -> Project context
  |     JWT access token -> User context
  |     Admin API key    -> Platform recovery (deprecated for day-to-day admin)
  |
  +-- AuthZ (org-scoped)
  |     Membership role -> Permission set
  |
  +-- Governance (optional)
  |     Prompt injection
  |     PII detection / redaction
  |     Input moderation
  |
  +-- Budget + rate limits
  |
  +-- Routing / fallback / retries / circuit breaker
  |
  +-- Provider call (OpenAI, Anthropic, OpenRouter, Groq, Ollama)
  |
  +-- Output security (secret scan, optional moderation)
  |
  +-- Cache write + usage logs + security audit events
  |
  v
Response (+ SSE stream path for /v1/chat/completions/stream)
```

## Trust boundaries

```text
+-------------------------------------------------------------+
| Public internet                                             |
|   Untrusted. Client applications, browsers, attackers.      |
+----------------------------+--------------------------------+
                             | TLS (ports 80/443 only)
+----------------------------v--------------------------------+
| Caddy reverse proxy (frontend network)                      |
|   Terminates TLS, sets security headers, hides upstreams.   |
+----------------------------+--------------------------------+
                             | HTTP, container network only
+----------------------------v--------------------------------+
| TokenGuard API + dashboard (frontend + backend networks)    |
|   First point that authenticates and authorizes a caller.   |
|   Holds the master key in memory; decrypts credentials.     |
+----------------------------+--------------------------------+
                             | internal network, no host ports
+----------------------------v--------------------------------+
| PostgreSQL, Redis, Celery workers (backend network)         |
|   Trusted. Never reachable from the internet.               |
+-------------------------------------------------------------+

Crossing outward:
  External providers (OpenRouter, OpenAI, Anthropic, Groq)
    Untrusted sinks. Receive prompts, so governance runs first.
  Local Ollama host
    Trusted only as far as the host is; it has no authentication.
```

The API is the only component that authenticates callers, and the only one
holding `TOKEN_GUARD_MASTER_KEY`. A compromise of the database alone does not
yield usable provider credentials; a compromise of the API process does.

## Authentication model

| Caller type              | Credential                         | Typical routes                          |
|--------------------------|------------------------------------|-----------------------------------------|
| Application / agent      | `X-TokenGuard-Key: tg_live_...`    | Chat, streaming, project-scoped APIs    |
| Human administrator      | `Authorization: Bearer <JWT>`      | Auth, orgs, members, org projects       |
| Emergency / bootstrap    | `X-TokenGuard-Admin-Key`           | Legacy `/v1/projects`, security events  |

JWT access tokens are short-lived. Refresh tokens are opaque, stored hashed,
and rotated on every use.

### Login and refresh

```text
POST /v1/auth/login
  verify Argon2 password hash
  -> access token  (JWT, 15 minutes, signed HS256)
  -> refresh token (opaque, stored as SHA-256)

POST /v1/auth/refresh
  look up hash -> already used?
     yes -> revoke the entire token chain (assume theft)
     no  -> issue a new pair, mark the old one used
```

### Project key resolution

```text
X-TokenGuard-Key
  -> SHA-256
  -> join project_api_keys with projects
     require: key active, key unexpired, project active
  -> bind project_id into the request context
```

The project ID bound here scopes every subsequent query, cache key, metric
label, and log record for the request.

## Request flows

### Chat completion

```text
authenticate project
  -> governance on input   (model policy, injection, PII)
  -> budget and rate limits
  -> routing decision      (tier, provider, model)
  -> exact cache lookup    -> hit? return, no provider call
  -> semantic cache lookup -> hit? return, no provider call
  -> provider call         (retry, circuit breaker, fallback)
  -> governance on output  (secret scan)
  -> cache write, request log, metrics, audit events
```

Governance runs before caching so a blocked prompt never reaches a cache
lookup, and output scanning runs before the cache write so unsafe content is
never stored.

### Exact cache

```text
key = SHA-256(project_id + provider + model + normalized messages + parameters)
```

The project ID is part of the key, which is what makes cross-project cache
leakage structurally impossible rather than merely checked.

### Semantic cache

```text
embed prompt (sentence-transformers)
  -> compare against this project's recent vectors
  -> best cosine similarity >= SEMANTIC_CACHE_THRESHOLD (0.85)?
       yes -> return the stored response, record the similarity score
       no  -> generate, then store prompt + embedding + response
```

Candidates are partitioned by project and by a fingerprint of the system
prompt and parameters, so a similar question under different instructions does
not collide.

### Routing

```text
score prompt complexity (length, requested output size, task indicators)
  + routing_preference (cost | balanced | quality)
  -> tier: simple | balanced | advanced
  -> provider + model from config/routing_policy.json
```

With `auto_route=false` the caller's provider and model are used as given and
the tier is `manual`.

### Fallback and retries

```text
attempt provider
  transient error?  -> retry with exponential backoff (bounded)
  repeated failure? -> circuit breaker opens, provider skipped entirely
  still failing?    -> next provider in the fallback chain
  all exhausted?    -> 503, with every attempt recorded
```

Retries are limited to transient errors; a 401 or a policy rejection is not
retried. Each attempt is written to `provider_attempt_logs`, so the response's
`reliability` block is reconstructable after the fact.

### Streaming

```text
same pre-flight checks as a non-streaming request
  -> SSE: metadata -> token* -> usage -> completed
     heartbeat comments while idle
     failure mid-stream -> error event, and nothing is cached
```

A rejection decided before generation is returned as an HTTP error status, not
as an SSE `error` event.

### Background jobs

```text
POST /v1/jobs/...  -> row in background_jobs (queued) + Celery task
worker             -> running, progress persisted for polling clients
                   -> completed | failed
GET /v1/jobs/{id}  -> scoped to the owning project (404 otherwise)
```

`task_acks_late` means a killed worker causes redelivery rather than loss, so
tasks are written to be idempotent.

### Analytics

Read directly from `llm_request_logs` and the aggregates that Celery Beat
maintains, always filtered by the authenticated project.

## Multi-tenancy

- **Organizations** isolate users and projects.
- **Memberships** assign roles: `owner`, `admin`, `developer`, `analyst`, `viewer`.
- **Permissions** (e.g. `projects:create`, `members:manage`) gate endpoints.
- Project lookups on org routes always filter by both `project_id` and
  `organization_id`.

## Persistence and workers

| Component   | Role                                              |
|-------------|---------------------------------------------------|
| PostgreSQL  | Source of truth (users, orgs, projects, logs)     |
| Redis       | Exact cache, rate limits, Celery broker/backend   |
| Celery      | Async jobs, usage aggregation, cleanup            |
| Alembic     | Schema migrations                                 |

Redis is deliberately not authoritative for anything. Credentials, projects,
users, budgets, and audit history live only in PostgreSQL, so a Redis flush
costs cache warmth and nothing else. This is what makes the degraded mode in
`docs/operations.md` safe.

## Observability

- Structured JSON logs with correlation IDs, with secrets redacted
- Prometheus metrics at `/metrics`, defined in `app/observability/metrics.py`
- Alert rules in `observability/alert_rules.yml`
- OpenTelemetry traces exported via OTLP (Jaeger in the Compose stack)

A single `X-Correlation-ID` ties together the structured log lines, the request
log row, the audit events, and the trace for one request.

## Related documents

- [security.md](security.md) — threat model, controls, and known limitations
- [api-guide.md](api-guide.md) — request and response contracts
- [deployment.md](deployment.md) — how this runs in production
- [operations.md](operations.md) — day-2 runbooks
- [performance.md](performance.md) — load, stress, and soak testing
