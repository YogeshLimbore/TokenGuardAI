# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 1.4.x   | Yes       |
| 1.3.x   | Yes (security fixes) |
| < 1.3   | No        |

## Reporting a vulnerability

If you discover a security issue in TokenGuard AI, please report it privately.

1. **Do not** open a public GitHub issue for vulnerabilities that could lead to
   credential theft, unauthorized access, or data exposure.
2. Email the maintainers with:
   - A short description of the issue
   - Steps to reproduce (proof-of-concept if available)
   - Affected version / commit
   - Your preferred contact for follow-up
3. Allow up to **7 days** for an initial response and **90 days** for a fix
   before public disclosure, unless we agree otherwise.

If this repository has GitHub Security Advisories enabled, you may also use
**Report a vulnerability** on the Security tab.

## Security design principles

TokenGuard AI is built around these invariants:

- Passwords are hashed with **Argon2**; plaintext passwords are never stored or logged.
- Refresh tokens are stored as **SHA-256 hashes** and rotated on every use.
- Access tokens are short-lived JWTs that carry only identity claims
  (`sub`, `type`, `jti`, `iat`, `nbf`, `exp`, `iss`, `aud`).
- Provider API keys are encrypted at rest with Fernet (`TOKEN_GUARD_MASTER_KEY`).
- Project API keys are stored hashed; the plaintext is shown only once at creation.
- Authorization is permission-based (RBAC), not ad-hoc role string checks.
- Cross-organization resource access is blocked by scoping lookups with
  `organization_id`.
- Login and refresh endpoints are rate-limited.
- Accounts lock after repeated failed authentication attempts.
- Application logs must never include passwords, JWTs, refresh tokens,
  `Authorization` headers, or provider secrets.

## Production checklist

Before exposing TokenGuard AI to the internet:

- [ ] `ENVIRONMENT=production` and `DEBUG=false`
- [ ] Unique `JWT_SECRET_KEY` (≥ 48 characters) — not reused for any other secret
- [ ] Unique `TOKEN_GUARD_MASTER_KEY`
- [ ] `BOOTSTRAP_ADMIN_PASSWORD` removed after first admin creation
- [ ] `ALLOW_PUBLIC_REGISTRATION=false`
- [ ] TLS terminated at a reverse proxy; set `FORCE_HTTPS_REDIRECT=true` if appropriate
- [ ] `ALLOWED_HOSTS` restricted to your real hostnames
- [ ] `CORS_ALLOWED_ORIGINS` limited to known dashboard/front-end origins
- [ ] OpenAPI docs disabled (`ENABLE_DOCS=false` or production defaults)
- [ ] Postgres and Redis not published to the public internet
- [ ] Admin API key rotated and stored in a secret manager

## Dependency scanning

CI runs `pip-audit` on every pull request. Locally:

```bash
uv sync --group dev
uv run pip-audit
```
