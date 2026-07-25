# TokenGuard AI

A secure, multi-tenant LLM gateway. Applications call TokenGuard instead of
calling providers directly, and TokenGuard authenticates the caller, enforces
budgets and governance, routes to an appropriate model, caches responses, falls
back when a provider fails, and records what happened.

Every response explains the decisions behind it — which model was chosen and
why, whether the cache was used, what the request cost, how much budget is
left, and what governance found — so the gateway is auditable rather than a
black box.

## Features

**Cost control**
- Exact-match and embedding-based semantic caching
- Complexity-based routing across cheap and capable models
- Per-project daily token and cost budgets, with automatic downgrade instead of
  outright rejection
- Per-request cost and token ceilings, and per-project rate limits

**Security**
- Multi-tenant isolation at the organization and project level
- Argon2 passwords, JWT access tokens, refresh-token rotation with reuse
  detection
- Provider credentials encrypted with Fernet; project API keys hashed
- Prompt-injection detection, PII redaction, and output secret scanning
- Security audit trail that stores categories, not raw content

**Reliability**
- Automatic provider fallback, bounded retries for transient errors only
- Circuit breakers and provider health scoring
- Degrades rather than fails when Redis is unavailable

**Operations**
- Prometheus metrics, OpenTelemetry traces, structured secret-safe logs
- Celery workers for background jobs, aggregation, and cleanup
- SSE streaming
- Streamlit analytics dashboard
- Docker Compose deployment with a Caddy reverse proxy and automatic HTTPS

## Stack

Python 3.13 · FastAPI · SQLAlchemy · PostgreSQL · Redis · Celery · Alembic ·
Streamlit · Prometheus · Grafana · OpenTelemetry · Docker · [uv](https://github.com/astral-sh/uv)

Providers: Ollama (local), OpenRouter, OpenAI, Anthropic, Groq.

## Quick start

Requirements: Python 3.13, `uv`, Docker Desktop, and optionally
[Ollama](https://ollama.com) for local models.

```bash
git clone <repository-url>
cd TokenGuardAI
cp .env.example .env
```

Generate the two required secrets and put them in `.env`:

```bash
# TOKEN_GUARD_MASTER_KEY - encrypts stored provider credentials
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# JWT_SECRET_KEY
uv run python -c "import secrets; print(secrets.token_urlsafe(64))"
```

Then bring up the whole stack:

```bash
docker compose up -d --build
docker compose ps
```

| Service | URL |
|---|---|
| API | http://localhost:8000 |
| Health | http://localhost:8000/health |
| API docs | http://localhost:8000/docs |
| Metrics | http://localhost:8000/metrics |
| Dashboard | http://localhost:8501 |
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:3000 |
| Jaeger | http://localhost:16686 |
| PostgreSQL | `localhost:5433` from the host, `postgres:5432` in-network |
| Redis | `localhost:6379` |

PostgreSQL is published on **5433** to avoid colliding with a local install;
container-to-container traffic still uses 5432. The API container runs
`alembic upgrade head` before starting, so the schema is current on startup.

Stop with `docker compose down`. Adding `-v` also deletes the volumes, and
therefore the database.

### Create the first administrator

```bash
# temporarily in .env
BOOTSTRAP_ADMIN_EMAIL=admin@example.com
BOOTSTRAP_ADMIN_PASSWORD=<long-random-password>

docker compose exec api uv run python scripts/bootstrap_admin.py
```

Then clear `BOOTSTRAP_ADMIN_PASSWORD` and restart the API. From there, create
an organization, a project, and a project API key — see the
[API guide](docs/api-guide.md).

### Try it with demo data

To explore without wiring up a provider:

```bash
uv run python scripts/seed_demo.py
```

This creates a demo organization, project, API key, and a week of synthetic
usage history. It never creates a real provider credential and never calls a
provider.

### First request

```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "X-TokenGuard-Key: tg_live_..." \
  -H "Content-Type: application/json" \
  -d '{
        "provider": "ollama",
        "model": "llama3.2",
        "messages": [{"role": "user", "content": "Explain semantic caching."}],
        "auto_route": true,
        "routing_preference": "balanced"
      }'
```

## Running without Docker

```bash
docker compose up -d postgres redis   # or use your own instances
uv sync
uv run alembic upgrade head

uv run uvicorn app.main:app --reload
uv run streamlit run dashboard/streamlit_app.py
uv run celery -A app.workers.celery_app:celery_app worker --loglevel=INFO
uv run celery -A app.workers.celery_app:celery_app beat --loglevel=INFO
```

Point `DATABASE_URL` and `REDIS_URL` in `.env` at your instances. The worker and
beat processes are only needed for background jobs and scheduled aggregation.

## Database migrations

`alembic/env.py` reads the URL from application settings, so `alembic.ini` never
needs editing.

```bash
uv run alembic upgrade head                              # apply
uv run alembic revision --autogenerate -m "describe it"  # create
uv run alembic current                                   # applied revision
uv run alembic check                                      # detect model drift
```

Always review a generated migration before applying it — autogenerate misses
server defaults, index renames, and constraint changes.

## Testing

```bash
uv run pytest -q                    # everything
uv run pytest -m unit               # fast, isolated
uv run pytest -m integration        # needs PostgreSQL and Redis
uv run pytest -m security           # isolation and attack tests
uv run pytest -m e2e                # full workflows
```

Tests run against `tokenguard_test` and a separate Redis database;
`tests/conftest.py` refuses to run against anything else. Do not use `-n auto`
for the integration and end-to-end suites, which share database state.

Run every gate CI enforces — lint, formatting, types, Bandit, `pip-audit`,
tests with an 85% coverage floor, migration drift, and release files:

```powershell
.\scripts\quality_check.ps1
```

Load, stress, and soak testing are covered in
[docs/performance.md](docs/performance.md).

## Documentation

| Document | Contents |
|---|---|
| [Architecture](docs/architecture.md) | Request flows, trust boundaries, caching and routing internals |
| [API guide](docs/api-guide.md) | Authentication, request and response fields, streaming, errors |
| [Deployment](docs/deployment.md) | CI/CD, production stack, migrations, rollback, TLS |
| [Security](docs/security.md) | Secret storage, controls, and known limitations |
| [Operations](docs/operations.md) | Runbooks, backups, failure drills, incident response |
| [Troubleshooting](docs/troubleshooting.md) | Symptoms, causes, and confirming commands |
| [Performance](docs/performance.md) | Load, stress, and soak testing |
| [Changelog](CHANGELOG.md) | Release history |
| [Contributing](CONTRIBUTING.md) | Development setup and PR expectations |

## Production

See [docs/deployment.md](docs/deployment.md) for the full procedure. In short:

```bash
cp .env.production.example .env.production   # fill in every replace_me
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  --env-file .env.production up -d
```

The production overlay pulls released images from GHCR, keeps PostgreSQL and
Redis on an internal network with no published ports, requires a Redis
password, and puts a Caddy reverse proxy with automatic HTTPS in front.

Two things worth stating plainly before going live:

- **Back up `TOKEN_GUARD_MASTER_KEY` separately from the database.** It
  decrypts every stored provider credential. Losing it makes them
  unrecoverable; storing it beside the backups defeats the encryption.
- **Never commit `.env`, `.env.production`, project API keys, JWT secrets, or
  encryption keys.** `.gitignore`, `.gitleaks.toml`, and
  `scripts/verify_release.py` all guard against this, but they are a safety
  net, not a substitute for care.

The security model, including its documented limitations, is in
[docs/security.md](docs/security.md).

## License

[MIT](LICENSE).
