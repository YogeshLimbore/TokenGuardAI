# Security model

How TokenGuard stores secrets, what it enforces, and — equally important —
what it does not protect against. For reporting a vulnerability, see
[SECURITY.md](../SECURITY.md).

## Secret storage

| Secret | Protection | Reversible? |
|---|---|---|
| User passwords | Argon2id via `pwdlib` (`app/services/password_service.py`) | No |
| Project API keys | SHA-256 (`app/services/project_key_service.py`) | No |
| Admin recovery keys | SHA-256 (`app/services/admin_service.py`) | No |
| Refresh tokens | SHA-256 (`app/services/token_service_jwt.py`) | No |
| Provider API credentials | Fernet (AES-128-CBC + HMAC) via `TOKEN_GUARD_MASTER_KEY` | Yes, by design |

API keys and refresh tokens are hashed rather than key-derived on purpose:
they are 32 bytes of `secrets`-generated entropy, so they are not brute
forceable and do not need a slow KDF. Passwords are user-chosen and low
entropy, so they do get one.

Provider credentials must be reversible — the gateway has to present the real
key upstream — so they are encrypted, not hashed. Two consequences:

- **`TOKEN_GUARD_MASTER_KEY` is the crown jewel.** Anyone holding it plus a
  database dump can recover every stored provider credential. Keep it in a
  secrets manager, not alongside the database backups.
- **Losing it is unrecoverable.** There is no escrow. Stored credentials
  become permanently undecryptable and must be re-entered by hand.

Rotating the master key requires re-encrypting every stored credential; there
is no automated migration for this yet.

## Authentication

Two independent paths, each for a different caller:

**JWT for humans and administration.** Access tokens are short-lived
(`ACCESS_TOKEN_EXPIRE_MINUTES`, 15 by default) and validated for signature,
expiry, issuer, and audience. `token_type` is checked so an access token
cannot be used where a refresh token is required, or the reverse.

**Refresh-token rotation with reuse detection.** Each refresh consumes the old
token and issues a new one. Presenting an already-used token revokes the
entire chain, on the assumption that it was stolen. Only hashes are stored, so
a database compromise does not yield usable tokens.

**Project API keys for traffic.** Long-lived, project-scoped, optionally
expiring, looked up by hash. They authenticate a project, not a person, and
carry no organization permissions.

Login and refresh are rate limited independently
(`AUTH_LOGIN_RATE_LIMIT_PER_MINUTE`, `AUTH_REFRESH_RATE_LIMIT_PER_MINUTE`),
and repeated failures lock the account.

## Authorization

Roles are enforced by `app/dependencies/authorization.py`, which resolves the
caller's membership in the organization named in the path and checks the
required permission. Platform admins bypass the permission check.

Roles and their permission sets are defined in `app/security/permissions.py`:

| Role | Capabilities |
|---|---|
| `owner` | Every permission, including organization deletion |
| `admin` | Manage members, projects, keys, credentials, and governance policy |
| `developer` | Read projects, manage credentials, create and cancel jobs |
| `analyst` | Read-only, including analytics and security events |
| `viewer` | Read-only, narrowest set |

### Tenant isolation

Every project-scoped query filters by the authenticated project, and every
organization-scoped query filters by verified membership. Cross-tenant access
returns **404, not 403**, so an attacker cannot use the status code to
enumerate which IDs exist.

Isolation is enforced at these boundaries, each with a corresponding test in
`tests/security/`:

- Organizations: reading, member listing, project creation, analytics, audit
  events
- Projects: credentials, API keys, background jobs, analytics
- Caches: both the exact and semantic caches include the project ID in the key,
  so one project can never be served another's cached response

## Request governance

Applied in order, before a request reaches a provider:

1. **Model policy.** The requested model must be in the project's allowed
   list.
2. **Prompt-injection detection.** Pattern and heuristic scoring. Above
   `PROMPT_INJECTION_BLOCK_THRESHOLD` the request is blocked; above
   `PROMPT_INJECTION_REVIEW_THRESHOLD` it is flagged and audited.
3. **PII detection.** Microsoft Presidio finds entities; the action is
   `redact` or `block` per `PII_DEFAULT_ACTION`. Redaction rewrites the prompt
   before it leaves the gateway.
4. **Optional provider moderation.** Off by default; costs an extra API call.
5. **Budget and rate limits.** Per-project daily token and cost budgets, plus
   per-request ceilings and a requests-per-minute limit.

On the response path, **output secret scanning** looks for credential-shaped
strings. A flagged response is never returned to the caller and never cached.

## Audit logging

Security-relevant events are written to `security_audit_events` with the event
type, severity, action, provider, model, risk score, and detected categories.

Raw prompt and response content is **not** stored while
`STORE_RAW_SECURITY_CONTENT=false` (the default). Only a sanitized summary and
the finding categories are kept, so the audit trail cannot itself become a PII
store. Enable raw storage only with a deliberate retention decision.

Structured logs are secret-safe: credentials, keys, and tokens are redacted by
`app/observability/logging.py`, and Presidio's own loggers are pinned to
`WARNING` so its debug output cannot echo detected PII. Health-check failures
log the driver error server-side but return a fixed string to the caller,
because driver errors embed hostnames and sometimes DSN credentials.

## Transport and headers

In production, Caddy terminates TLS and sets HSTS, `X-Content-Type-Options`,
`X-Frame-Options`, and `Referrer-Policy`, and strips `Server`
(`deploy/Caddyfile`). The application enforces `ALLOWED_HOSTS` and an explicit
CORS origin list. PostgreSQL and Redis sit on an internal Docker network with
no published ports.

## Supply chain

Every push and a weekly schedule run Bandit (SAST), `pip-audit` (dependency
advisories), Gitleaks (secret scanning, with narrow allowlists for synthetic
test values in `.gitleaks.toml`), and Trivy against both images, failing on
fixable HIGH or CRITICAL findings. Dependencies are pinned in `uv.lock` and CI
installs with `--frozen`.

## Known limitations

Stated plainly, because a security document that claims completeness is
useless:

- **Prompt-injection detection is pattern-based and incomplete.** It
  normalizes case and whitespace only. Base64-encoded payloads, Unicode
  homoglyphs, and zero-width-space obfuscation all get through; these are
  recorded as `xfail` tests in
  `tests/security/test_prompt_injection_variations.py` rather than hidden.
  Treat it as one layer among several, never as a guarantee.
- **PII detection produces false positives and false negatives.** Presidio is
  statistical. Do not rely on it as the sole control for regulated data.
- **Output secret scanning matches known credential shapes.** A novel or
  encoded secret format will pass.
- **Ollama has no authentication of its own.** Anything that can reach the
  Ollama port can use it. Keep it on a private network.
- **Admin recovery keys are shared secrets.** They cannot be attributed to an
  individual. Use per-user JWT auth and keep the recovery key for
  break-glass only; see [migration-admin-to-jwt.md](migration-admin-to-jwt.md).
- **The Streamlit dashboard has weak session management.** It is suitable for
  operators on a trusted network, not for public exposure.
- **Cost figures are estimates.** They come from `config/model_pricing.json`
  and drift as providers change pricing. Do not use them for billing.
- **Token counts for some providers are estimated,** not returned by the
  provider, so budget accounting is approximate at the margins.
- **Rate limiting degrades under a Redis outage.** It falls back to
  database-backed counting, which is coarser and slower.
- **No automated master-key rotation.** Rotation is a manual, planned
  re-encryption.

## Production checklist

```text
[ ] TOKEN_GUARD_MASTER_KEY generated fresh, stored in a secrets manager
[ ] JWT_SECRET_KEY at least 48 random characters, unique to this deployment
[ ] POSTGRES_PASSWORD and REDIS_PASSWORD set to strong unique values
[ ] ALLOWED_HOSTS restricted to real hostnames, never "*"
[ ] CORS_ALLOWED_ORIGINS restricted to the dashboard origin
[ ] DEBUG=false and ENABLE_DOCS=false
[ ] REQUIRE_PROJECT_AUTHENTICATION and REQUIRE_ADMIN_AUTHENTICATION true
[ ] ALLOW_PUBLIC_REGISTRATION=false
[ ] STORE_RAW_SECURITY_CONTENT=false
[ ] POLICY, BUDGET, and RATE_LIMITING enforcement all enabled
[ ] BOOTSTRAP_ADMIN_PASSWORD cleared after the first admin is created
[ ] TLS verified, HSTS present, PostgreSQL and Redis not publicly reachable
[ ] Backups running, and a restore actually tested
[ ] Master key backed up separately from the database backups
```
