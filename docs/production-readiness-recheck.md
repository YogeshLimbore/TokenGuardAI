# TokenGuardAI production remediation report

Reviewed and remediated 12–14 October 2026. This report supersedes the
[historical baseline review](production-readiness-review.md) and the earlier
unfinished-work recheck. It describes the current working tree; it is not a
claim that an untested production environment has been certified.

## Current decision

The source tree now passes its release gates and the previously reproduced P1
application defects have been repaired. It is suitable for a controlled staging
deployment. Public production launch still requires environment-specific load,
restore, alert-delivery, and real-provider acceptance tests. Enterprise buyers
may also require SSO/MFA, invitation email, and password-recovery workflows.

## Verified evidence

- Alembic upgraded a disposable PostgreSQL 16 database through every migration,
  including deterministic session invalidation and project model pricing.
  `alembic check` reports no ORM/schema drift.
- The isolated PostgreSQL/Redis suite passes: **453 passed**, zero failures,
  zero expected failures, and **85.55% branch coverage** against an 85% gate.
- Ruff lint and formatting pass across 176 Python files.
- MyPy passes across 116 application, dashboard, and operational source files.
- Both exact locked runtime sets (API and dashboard) report **no known
  vulnerabilities among auditable packages** in pip-audit. The CPU-only Torch
  wheel and direct spaCy model artifact are outside PyPI's audit resolution and
  are explicitly reported as skipped; registry image/SBOM scanning remains a
  launch gate. The local development environment's copy of pip is build tooling
  and is excluded from both images.
- Bandit reports no medium- or high-severity findings. Its nine low findings are
  reviewed false positives or intentional demo/release-script behavior.
- Production Compose renders successfully using the documented example values.
- The API and dashboard Dockerfiles build successfully, run as the non-root
  `tokenguard` user, and install frozen dependencies without runtime syncing.
- The API image uses CPU-only PyTorch and includes the small English spaCy model.
  An in-image PII smoke test redacted an email successfully.
- The dashboard image installs only its own runtime group. Its verified image is
  about 294 MB instead of carrying the API's ML and worker dependencies.

Evidence files are retained under
`C:/Users/Yogesh/.codex/visualizations/2026/10/12/01a130b3-668d-7a33-9a18-9107a832ee08/recheck/`.
The tests used only the disposable containers labelled
`tokenguard.fix=critical2`, PostgreSQL port 55433, and Redis port 56379.

## Remediated areas

### Authentication and tenant authorization

- Access tokens carry a database-backed session version. Password changes
  increment it and immediately invalidate issued access tokens and refresh
  sessions.
- Refresh rotation is row-locked; replay revokes the user's refresh sessions.
  Users can list, revoke one, or revoke all sessions.
- Password changes verify the current password and are audited.
- The bootstrap command reads a hidden prompt or mounted file. No plaintext
  bootstrap password is accepted through Settings, CLI arguments, or normal
  environment configuration. Bootstrap administrators are email-verified.
- Platform administrators can provision and list verified users even when
  public registration is disabled.
- Organization membership changes transactionally protect the final active
  owner.
- Project API keys use explicit scopes for chat, analytics, credentials,
  governance, jobs, and security events. JWT project access uses organization
  membership and role permissions.

### Multi-provider operation

- One registry supports Ollama, OpenAI, Anthropic, OpenRouter, Groq, Gemini,
  DeepSeek, Mistral, Together, Fireworks, xAI, and Perplexity.
- Operators can add additional HTTPS OpenAI-compatible endpoints through
  validated `PROVIDER_CONFIGS`. Embedded credentials, query strings, insecure
  URLs, duplicate names, and collisions with built-ins are rejected.
- Projects can store multiple encrypted credentials, select a default, test a
  credential, and discover models without exposing the plaintext key.
- Native and compatible providers implement streaming. In-band errors,
  incomplete streams, transport failures, status failures, and oversized
  responses are normalized and sanitized.
- Project-scoped exact or wildcard model pricing is available through API and
  dashboard. Admission remains fail-closed for paid models without pricing.
  Project prices drive estimates, settlement, cache savings, and model
  discovery status.

### Budgets, fallback, and accounting

- Every provider attempt reserves usage under a locked project row. Retries and
  fallbacks cannot independently reuse the caller's request-wide token or cost
  allowance.
- A total provider deadline bounds the complete retry/fallback operation.
  Timeout failures are logged and can proceed to eligible fallbacks.
- Local Ollama fallback is explicit opt-in. Every destination is checked against
  current project governance before dispatch.
- Output limits admitted by the budget service are passed upstream.
- Returned provider model names can fall back to the requested billing alias.
- Uncertain calls retain reservations instead of silently releasing possible
  spend.
- Immutable request logs now store tokens and cost avoided; analytics and
  aggregation use those snapshots and backfill recent dates.

### Governance, cache, and workers

- Input/output moderation fails closed when enabled but unavailable, and
  production startup rejects an enabled moderation service without a key.
- Streaming buffers complete output before governance checks, so unsafe partial
  bytes cannot be retracted after delivery.
- Prompt-injection normalization covers Unicode confusables, zero-width
  characters, split-letter words, common visual substitutions, and bounded
  Base64 text. All former known-bypass tests now pass.
- File and database project policy are combined deterministically. Authorized
  users can read, update, audit, and disable project policy.
- Cache keys include full conversation and policy state. Expiry is enforced,
  exact replacement refreshes accounting fields, semantic hit increments are
  atomic, and cached output is governed before reuse.
- Job publication failure marks the row failed. Active-job, dataset byte, and
  example quotas are enforced. Worker claims are atomic and running jobs
  cooperate with cancellation.

### Deployment and recovery

- Docker context excludes environment files, keys, certificates, dumps,
  backups, editor data, and repository metadata.
- Images pin uv, sync frozen dependencies during build, remove pip, and run
  without root.
- CPU-only PyTorch avoids unused CUDA libraries. The PII language model is an
  explicit locked dependency.
- `/live` reports process liveness and `/ready` returns 503 until PostgreSQL
  and Redis are ready. Compose and deployment verification use `/ready`.
- Production Compose aligns database credentials, protects Redis, isolates
  backend networking, applies resource/log limits, and keeps metrics off the
  public Caddy route.
- Deployment uses the production env file, requires immutable image tags,
  checks out the exact target revision, verifies OCI revision labels, and does
  not ignore pull failures.
- Restore testing requires an explicitly named scratch database, validates
  resolved paths, stops on native-command failures, and checks restored data.
- Destructive tests require explicit opt-in plus parsed local test database and
  non-zero Redis targets.

## Required before a public launch

These items depend on the target environment and cannot be proven by repository
tests alone:

1. Run the documented load, stress, soak, disconnect, and graceful-shutdown
   scenarios at expected tenant concurrency and record capacity thresholds.
2. Exercise one paid request and one streamed request against each provider
   protocol actually enabled for launch. Confirm current model prices, quotas,
   error contracts, and provider-specific headers.
3. Restore an encrypted production-like backup on separate infrastructure and
   measure recovery point and recovery time.
4. Connect Prometheus alerts to an owned notification channel and prove delivery,
   acknowledgement, and escalation.
5. Run image/SBOM/container scanning in the release registry and deploy the
   exact tested image digest to staging before promotion.
6. Decide whether the launch audience requires invitation email, password reset,
   email-verification delivery, MFA, SSO, SCIM, Azure OpenAI deployment auth,
   Bedrock IAM, or Vertex service accounts. Generic bearer-compatible endpoints
   do not replace those native enterprise integrations.

No code review can guarantee “best in market.” The current tree has a defensible,
tested production foundation; the remaining work is release validation and
product differentiation rather than the previously reproduced control failures.
