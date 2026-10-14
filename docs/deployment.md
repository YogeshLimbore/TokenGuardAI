# Deployment

Running TokenGuard AI beyond local development: CI/CD, the production stack,
migrations, and rollback.

## Environments

| Environment | Compose files | Notes |
|---|---|---|
| Development | `docker-compose.yml` | Images built locally, ports published, docs enabled |
| Production | `docker-compose.yml` + `docker-compose.prod.yml` | Registry images, only 80/443 published, `DEBUG=false`, docs off |

The production file is an **overlay**, not a standalone stack. Always pass both
`-f` flags; passing only the overlay will fail because the service definitions
live in the base file.

What the overlay changes:

- Images are pulled from GHCR instead of built (`TOKEN_GUARD_API_IMAGE`,
  `TOKEN_GUARD_DASHBOARD_IMAGE`)
- Only the Caddy reverse proxy publishes ports; PostgreSQL, Redis, the API, and
  the dashboard publish none
- PostgreSQL and Redis move to an `internal: true` Docker network, so they are
  unreachable from the host and the internet
- Redis requires a password
- `env_file` points at `.env.production`, so a stray development `.env` on the
  host cannot override production configuration
- Grafana, Jaeger, Prometheus, and the OTEL collector stay behind the
  `observability` profile

## CI/CD

| Workflow | Trigger | Purpose |
|---|---|---|
| `ci.yml` | push and PR | Ruff lint, Ruff format, mypy, migrations, `alembic check`, pytest with an 85% coverage floor, image build |
| `security.yml` | push, PR, weekly | Bandit, `pip-audit`, Gitleaks, Trivy on both images |
| `docker.yml` | push to `main`, `v*.*.*` tags | Publishes images to GHCR |
| `release.yml` | `v*.*.*` tags | Creates the GitHub Release |
| `deploy.yml` | manual | Deploys to staging or production over SSH |

All gates in `ci.yml` are blocking. Run the same set locally before pushing:

```powershell
.\scripts\quality_check.ps1
```

The security workflow also runs weekly, because a dependency advisory can land
without anyone touching the repository.

### Required GitHub configuration

Create the `staging` and `production` environments under
**Settings → Environments** and add a required reviewer to `production`, which
is what makes production deploys need an explicit approval.

Per-environment secrets for `deploy.yml`:

| Secret | Purpose |
|---|---|
| `DEPLOY_SSH_PRIVATE_KEY` | Key authorized on the target host |
| `DEPLOY_HOST` | Hostname or IP |
| `DEPLOY_USER` | SSH user |
| `DEPLOY_PATH` | Absolute path to the checkout, e.g. `/opt/tokenguard` |
| `API_BASE_URL` | Public base URL, e.g. `https://api.example.com` |
| `SMOKE_TEST_PROJECT_KEY` | Optional; enables an authenticated smoke check |

`docker.yml` needs no secrets — it uses the built-in `GITHUB_TOKEN`.

## First production deploy

### 1. Prepare the host

Docker Engine with the Compose plugin, DNS for both domains pointing at the
host, and ports 80 and 443 reachable so Caddy can complete the ACME challenge.

```bash
git clone <repository-url> /opt/tokenguard
cd /opt/tokenguard
cp .env.production.example .env.production
```

### 2. Fill in `.env.production`

Generate real secrets — never reuse the examples:

```bash
# TOKEN_GUARD_MASTER_KEY
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# JWT_SECRET_KEY
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

Set `API_DOMAIN`, `DASHBOARD_DOMAIN`, `TLS_CONTACT_EMAIL`, `ALLOWED_HOSTS`,
`POSTGRES_PASSWORD`, `REDIS_PASSWORD`, and pin both image tags to a released
version. Every `replace_me` must be gone; `verify_release.py` and
`deploy_production.ps1` both refuse to proceed otherwise.

Back up `TOKEN_GUARD_MASTER_KEY` to a secrets manager **before** storing any
provider credential. Losing it makes every stored credential permanently
undecryptable, and it must not live in the same place as the database backups.

### 3. Start the stack

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  --env-file .env.production up -d postgres redis

docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  --env-file .env.production run --rm api uv run alembic upgrade head

docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  --env-file .env.production up -d
```

With the observability stack:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  --env-file .env.production --profile observability up -d
```

### 4. Create the first administrator

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  --env-file .env.production exec api uv run --no-sync python \
  scripts/bootstrap_admin.py --email admin@example.com
```

The command prompts twice with hidden input. For non-interactive automation,
mount a secret file read-only and pass `--password-file`; remove the mount as
soon as bootstrap succeeds. Never place the password in an environment variable
or command argument.

### 5. Verify

```powershell
$env:TOKEN_GUARD_API_BASE_URL = "https://api.example.com"
uv run python scripts/smoke_test.py
```

The smoke test checks health, the metrics endpoint, and — importantly — that
an unauthenticated request to a project endpoint is actually rejected.

## Subsequent deploys

Either trigger `deploy.yml` from the Actions tab, or run on the host:

```powershell
.\scripts\deploy_production.ps1 -ImageTag 1.5.0
```

Both paths perform the same ordered steps:

```text
1. Back up PostgreSQL          <- before anything touches the schema
2. Pull the pinned images
3. Start PostgreSQL and Redis
4. Run alembic upgrade head
5. Start the application services
6. Poll the health endpoint
7. Run the smoke test
```

Step 1 comes first because that is the only point at which a backup can still
rescue a bad schema change.

## Migration safety

Never ship a destructive schema change in the same release as code that still
expects the old schema. During a rolling restart both versions run briefly, so
the schema must satisfy both.

For anything risky, expand and contract across releases:

```text
Release A  add the new nullable column
Release B  write to both old and new columns
Backfill   populate the new column for existing rows
Release C  read from the new column
Release D  drop the old column
```

`alembic check` runs in CI and fails when a model change has no corresponding
migration, which otherwise only surfaces during a deploy. Always read a
generated migration before committing it: autogenerate misses server defaults,
index renames, and constraint changes.

## Rollback

### Application

Images are immutable and tagged, so a rollback is a tag change:

```powershell
.\scripts\deploy_production.ps1 -ImageTag 1.4.0 -SkipBackup
```

Or edit `TOKEN_GUARD_API_IMAGE` and `TOKEN_GUARD_DASHBOARD_IMAGE` in
`.env.production` and bring the stack up again. Keep the last known-good tag
recorded somewhere outside the host.

This is safe only when the newer release did not migrate the schema in a way
the older code cannot read. That is exactly why expand-and-contract matters.

### Database

Do **not** reflexively run `alembic downgrade -1` in production. Many
migrations are not losslessly reversible: a dropped column cannot be restored
by a downgrade, and a data migration usually cannot be undone at all.

In preference order:

1. **Roll forward.** Ship a corrective migration. Almost always the right
   answer.
2. **Restore the backup** taken in step 1 of the deploy, accepting the loss of
   anything written since. See the restore procedure in
   [operations.md](operations.md).
3. **Downgrade**, only for a migration whose `downgrade()` has been tested
   against realistic data.

## TLS and domains

Caddy (`deploy/Caddyfile`) obtains and renews certificates automatically once
DNS points at the host and 80/443 are reachable. It also sets HSTS,
`X-Content-Type-Options`, `X-Frame-Options`, and `Referrer-Policy`, and strips
the `Server` header.

Two details worth knowing:

- Streaming endpoints are proxied with `flush_interval -1` and a long response
  timeout. Without that, buffering defeats server-sent events.
- The dashboard needs websocket upgrades, which Caddy's `reverse_proxy` handles
  by default.

While testing, uncomment `acme_ca` in the Caddyfile to use Let's Encrypt's
staging endpoint and avoid rate limits on repeated attempts.

Set `FORCE_HTTPS_REDIRECT=true` only once every client reaches the API over
HTTPS.

## Secrets

Store these in a secrets manager, not in the repository or the image:

```text
TOKEN_GUARD_MASTER_KEY      POSTGRES_PASSWORD
JWT_SECRET_KEY              REDIS_PASSWORD
ADMIN_API_KEY_HASH          provider API credentials
```

Never place secrets in a Dockerfile, in `docker-compose.yml`, in a workflow
file, or in source. Docker image layers are readable by anyone who can pull the
image, and build arguments persist in the image history.

`.env.production` is covered by `.gitignore` and by a rule in
`.gitleaks.toml`. `verify_release.py` fails if it ever becomes tracked.

## Monitoring

Alert rules are in `observability/alert_rules.yml`, and every expression uses a
metric that `app/observability/metrics.py` actually registers. Prometheus
evaluates them; routing them to a pager needs Alertmanager, which is not part
of the Compose stack. Grafana can evaluate the same expressions directly.

Validate before restarting Prometheus:

```powershell
docker run --rm -v "${PWD}/observability:/rules" `
    --entrypoint promtool prom/prometheus:latest `
    check rules /rules/alert_rules.yml
```

The thresholds are starting points. Tune them against a week of real traffic
before paging anyone: an alert that fires constantly gets muted, which is worse
than no alert.

## Scaling

- Scale API replicas behind the reverse proxy. Remove `container_name` from the
  base compose file first, since a fixed name prevents scaling.
- Scale workers: `docker compose ... up -d --scale worker=3`
- Run exactly one `beat` instance, or scheduled tasks fire multiple times.
- Prefer managed PostgreSQL and Redis in cloud deployments, and point
  `DATABASE_URL` and `REDIS_URL` at them instead of running the containers.

## AWS Free Tier staging

For a guarded single-host staging deployment on a new AWS Free account plan,
see [AWS Free Tier staging deployment](aws-free-tier-deployment.md). The script
refuses to provision resources on a paid, expired, or low-credit account.
