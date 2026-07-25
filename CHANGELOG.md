# Changelog

All notable changes to TokenGuard AI are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.5.0] — 2026-07-25

First release intended for production deployment: every quality gate is
blocking, the production stack terminates TLS, and the operational
documentation is complete.

### Added

- Test and security suite: end-to-end workflow tests, cross-organization and
  cross-project isolation tests, JWT attack tests, prompt-injection bypass
  tests, PII-leakage tests, and failure-degradation tests, with coverage
  raised past the 85% gate
- Static analysis and scanning: Bandit, `pip-audit`, Gitleaks with a narrow
  allowlist for synthetic test values, and Trivy against both images
- Locust load, stress, and soak scenarios in `load_tests/`
- PostgreSQL backup and restore-validation scripts
  (`scripts/backup_postgres.ps1`, `scripts/restore_test.ps1`), where restore
  validation asserts that credentials are still encrypted and keys still hashed
- Separate CI workflows: `ci.yml` (lint, format, types, migrations, tests),
  `security.yml` (scans, also weekly), `docker.yml` (GHCR publishing),
  `deploy.yml` (approval-gated SSH deploy with pre-migration backup)
- `alembic check` as a CI gate, so a model change without a migration fails
  before it reaches a deploy
- Caddy reverse proxy (`deploy/Caddyfile`) with automatic HTTPS, security
  headers, and SSE-aware proxying for streaming endpoints
- `.env.production.example` template and production secret-handling guidance
- Prometheus alert rules (`observability/alert_rules.yml`) covering
  availability, error rate, latency, provider failures, circuit breakers,
  governance blocks, and budget exhaustion
- `scripts/smoke_test.py`, `scripts/verify_release.py`,
  `scripts/seed_demo.py`, and `scripts/deploy_production.ps1`
- Documentation: `docs/api-guide.md`, `docs/security.md`,
  `docs/troubleshooting.md`; architecture expanded with request flows and
  trust boundaries; deployment expanded with CI/CD, migration safety, and
  rollback; operations expanded with runbooks for queues, breakers, and
  security events

### Changed

- mypy and `ruff format` are now blocking CI gates rather than advisory
- The production overlay pulls released images from GHCR, moves PostgreSQL and
  Redis to an internal network with no published ports, requires a Redis
  password, and reads `.env.production` so a stray development `.env` cannot
  override production configuration
- `GovernanceCheckResult` now enforces that a blocked check carries its audit
  attribution, replacing a path where an audit record could have been written
  with a missing event type or severity
- Budget policy values are validated when read, so a malformed
  `config/budget_policy.json` produces a clear configuration error instead of a
  `TypeError` at request time
- README rewritten around what the gateway does and how to operate it

### Removed

- `app/schemas/streaming.py`, which was unused

### Security

- Health-check responses no longer echo driver errors, which can embed
  hostnames and DSN credentials
- Presidio loggers pinned to `WARNING` so debug output cannot echo detected PII
- Upgraded `gitpython` to resolve reported advisories

## [1.4.0] — 2026-07-25

### Added

- HTTP security headers middleware (`X-Content-Type-Options`, `X-Frame-Options`,
  `Referrer-Policy`, HSTS when HTTPS is detected)
- Environment-aware CORS and optional `TrustedHostMiddleware`
- Production configuration guards (reject `DEBUG=true`, placeholder JWT secrets,
  and leftover bootstrap passwords when `ENVIRONMENT=production`)
- OpenAPI `/docs` and `/redoc` disabled by default in production
- Integration tests for login → organization → project flows
- Locust load-test scenarios under `tests/load/`
- GitHub Actions CI (lint, dependency audit, pytest, Docker build)
- GitHub Actions release workflow publishing images to GHCR on `v*` tags
- `docker-compose.prod.yml` production overlay with healthchecks and locked-down ports
- `scripts/migrate.sh` for pre-rollout Alembic migrations
- Documentation pack: architecture, deployment, operations, performance,
  `SECURITY.md`, `CONTRIBUTING.md`

### Changed

- Application version aligned to **1.4.0** across `pyproject.toml`, config, and docs

## [1.3.0] — 2026-07-24

### Added

- Enterprise identity: users, organizations, memberships, Argon2 passwords
- JWT access tokens + hashed, rotating refresh tokens
- Role-based access control with explicit permissions
- Organization-scoped project routes under `/v1/organizations/{id}/projects`
- Auth rate limiting on login and refresh
- Account lockout after repeated failed logins
- Identity audit events
- `scripts/bootstrap_admin.py`

## [1.2.0] — 2026-07

### Added

- Policy enforcement and content governance (prompt injection, PII, moderation,
  output secret scanning)
- Security audit event store and admin listing endpoint
- Streaming chat completions with SSE
- Background jobs via Celery (worker + beat)

## [1.1.0] — 2026-06

### Added

- Project API keys and admin authentication
- Budget enforcement and rate limiting
- Provider credentials with Fernet encryption
- Semantic and exact response caching
- Automatic model routing and provider fallback
- Prometheus metrics, OpenTelemetry tracing, correlation IDs

## [1.0.0] — 2026-05

### Added

- Initial FastAPI LLM gateway with multi-provider chat completions
- PostgreSQL persistence via Alembic
- Redis caching foundations
- Streamlit analytics dashboard
