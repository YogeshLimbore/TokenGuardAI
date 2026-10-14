# TokenGuardAI production-readiness review

> Historical baseline review. The findings below describe commit `59f5abb` and
> have since been remediated or reclassified. See the [current remediation
> report](production-readiness-recheck.md) for verified results and remaining
> launch work.

Reviewed 12–13 October 2026 · repository `D:/GenAI/TokenGuardAI` · commit `59f5abb`

## Decision

**Do not release the current revision as a public multi-tenant production gateway.** The repository has substantial working foundations, but several controls can be bypassed or do not behave as advertised. The highest-risk problems affect organization ownership, refresh sessions, output safety, hard spend limits, conversation caching, and the production/recovery pipeline.

This review records **48 actionable findings: 24 P1 and 24 P2**, plus missing features and cleanup work. These are grouped findings, not a claim that every possible bug has been discovered. P1 means resolve before exposing the affected production capability; several P1 findings have explicit configuration or workflow preconditions. P2 means correctness, operational reliability or maintainability work that should be closed before relying on the affected feature at scale. These priorities are remediation judgments, not CVSS scores.

Most findings are established by source/dataflow inspection. The report explicitly identifies reproduced behaviors, unverified concurrency implications and conditional risks. Scanner findings are separated from proven application vulnerabilities. No application fixes, deployment, dependency upgrades or secret rotations were performed.

## Scope and evidence

Reviewed API/authentication/authorization, providers and retry/fallback behavior, budgets/routing/pricing, governance, exact/semantic caching, database models/migrations, Celery jobs, analytics/dashboard, deployment/recovery, CI/security workflows, documentation and test structure.

The inventory contains 103 application Python files, 43 router-decorated HTTP route declarations and 377 test functions. The examined Python directories contain 174 files and approximately 30,412 lines. Inventory counts are not a coverage claim.

Validated so far:

- Ruff lint passed; formatting check reported 181 files already formatted.
- MyPy passed for 103 application source files.
- Bandit medium/high-severity scan completed successfully. This does not cover the business-logic defects below.
- Release verification script passed.
- 124 isolated tests passed across password/JWT/key services, budgets/routing, configuration, output/prompt checks, SSE helpers, provider clients/retries and rate limits. These were run with `--noconftest` and mocks/in-memory data so the shared Redis-flushing fixture was not invoked.
- Offline reproductions confirmed admin-to-owner promotion, active refresh-token descendants after replay, downgrade allowing an over-limit request, floating-point threshold nonprogress, and identical semantic inputs for different histories.
- The CI Fernet literal was locally decoded and rejected: 41 decoded bytes rather than 32.
- PostgreSQL downgrade compilation reproduced the unnamed-constraint failure.
- Dependency audit completed against public advisory data: 43 raw entries, **35 distinct package/advisory pairs across six packages**. See the dependency section.
- Full Docker-backed test run: **425 passed, 5 expected failures; 87.92% branch-inclusive coverage**, exceeding the configured 85% threshold. Pytest reported 30.79 seconds. Five migrations applied successfully; `alembic check` found no schema drift. This run used fresh isolated PostgreSQL/Redis, valid disposable secrets and locally installed/cached ML assets; it did not use the invalid CI Fernet literal.
- The five expected failures cover known prompt-injection obfuscation bypasses: character spacing, letter substitution, base64, Cyrillic homoglyphs and zero-width spaces. They are limitations, not passing security controls.
- The 124 isolated passes overlap the full suite; do not add them to 425 as a distinct-test total.

Paid external providers, a real public deployment, production data restore, internet-facing penetration tests and a sustained load/soak test were not exercised. Current cloud branch protections and deployed-image contents were not available as verified evidence. No exact production capacity or percentage-readiness score is claimed.

## What is already useful

Keep the modular provider interface, typed request/response schemas, SQLAlchemy/Alembic foundation, encrypted stored provider credentials, hashed project/refresh tokens, project-partitioned queries, JWT role infrastructure, Redis cache/rate-limit/breaker primitives, category-based security auditing, structured logging/correlation, metrics, and existing test/scanner workflows.

These foundations make repair practical. The central problem is consistency between layers: a check exists in one path but is absent from fallback, cache, streaming, concurrent execution or deployment. More documentation and more features will not close those gaps on their own.

## Detailed findings

Each item includes the trigger, consequence, recommended change and an acceptance check. File references point to the reviewed local revision; line numbers will move as fixes land.

### R01 · P1 · Public evaluation endpoint can stop an API worker

**Area:** Security. **Evidence:** POST /v1/evaluation/semantic-cache has no authentication dependency. Its threshold loop accepts any positive step up to 0.25 and advances a floating-point value by that step. The evaluation and embedding work run synchronously inside the async endpoint.

**Impact and conditions:** A sufficiently tiny positive step does not change a nonzero float, so the loop never terminates. Offline reproduction confirmed no progress with step=1e-300. Ordinary unauthenticated evaluation also permits expensive CPU/model work. This can exhaust API availability.

**Recommended change:** Require privileged, tenant-scoped authorization; use bounded integer-indexed threshold generation with a strict maximum count; move evaluation to a quota-controlled worker.

**Acceptance check:** Reject tiny steps/excessive combinations before loading models; unauthorized calls return 401/403; enforce execution time and memory limits.

**Source:** [app/api/evaluation.py:21](</D:/GenAI/TokenGuardAI/app/api/evaluation.py:21>), [app/api/evaluation.py:48](</D:/GenAI/TokenGuardAI/app/api/evaluation.py:48>).

### R02 · P1 · Organization administrators can promote themselves to owner

**Area:** Identity. **Evidence:** The owner-membership guard protects edits to an existing owner but does not restrict assigning the owner role to a currently non-owner member. Administrators have member-management permission.

**Impact and conditions:** An administrator can change their own membership to owner, or create another owner. The offline service/guard reproduction changed admin to owner. This defeats the intended role hierarchy.

**Recommended change:** Make owner assignment and ownership transfer owner-only; apply the rule to both creation and updates; retain last-owner protection inside a transaction.

**Acceptance check:** Test every actor-role/target-role transition, self-promotion, owner creation, last-owner removal and concurrent ownership transfers.

**Source:** [app/api/organizations.py:245](</D:/GenAI/TokenGuardAI/app/api/organizations.py:245>), [app/api/organizations.py:305](</D:/GenAI/TokenGuardAI/app/api/organizations.py:305>), [app/api/organizations.py:333](</D:/GenAI/TokenGuardAI/app/api/organizations.py:333>).

### R03 · P1 · Refresh rotation lacks replay-family revocation and atomic consumption

**Area:** Identity. **Evidence:** rotate_refresh_token reads the row without a lock/conditional consumption, rejects a revoked token, and issues a replacement. It does not revoke the replacement chain when an old token is reused.

**Impact and conditions:** Offline reproduction verified the replacement remains active after reuse of the old token. Concurrent refreshes can both observe an active record and create independent descendants; that race is a source-level finding, not a live PostgreSQL reproduction.

**Recommended change:** Consume the token atomically, represent a session/token family, and revoke the family on replay with an audit event. Define behavior for harmless client retry races.

**Acceptance check:** Two simultaneous refreshes yield at most one valid successor; replay after rotation invalidates the family; unrelated sessions remain valid.

**Source:** [app/services/identity_service.py:248](</D:/GenAI/TokenGuardAI/app/services/identity_service.py:248>), [app/services/identity_service.py:268](</D:/GenAI/TokenGuardAI/app/services/identity_service.py:268>).

### R04 · P1 · Streaming exposes output before its safety decision

**Area:** Governance. **Evidence:** Streaming yields provider token chunks immediately. evaluate_output runs only after generation ends. Error/disconnect branches can persist accumulated output before that scan.

**Impact and conditions:** A secret or prohibited content can reach the client before a later blocked result. The final safety event cannot retract already-delivered bytes. Partial output persistence also weakens the stated safe-storage behavior.

**Recommended change:** Define an enforceable streaming contract. Buffer the complete answer when full-response checks are required, or disable streaming for those policies. A rolling scanner alone cannot guarantee full-response moderation. Finalize partial output through the same privacy policy.

**Acceptance check:** Use a fake stream with a secret split across chunks; assert zero unsafe bytes reach the client or retained logs, including errors and cancellation.

**Source:** [app/api/streaming.py:819](</D:/GenAI/TokenGuardAI/app/api/streaming.py:819>), [app/api/streaming.py:824](</D:/GenAI/TokenGuardAI/app/api/streaming.py:824>), [app/api/streaming.py:1054](</D:/GenAI/TokenGuardAI/app/api/streaming.py:1054>).

### R05 · P1 · Reliability fallback skips destination model authorization

**Area:** Governance. **Evidence:** Input governance authorizes the initially selected provider/model. FallbackService always appends ollama/llama3.2, then executes attempts without rechecking destination policy; streaming executes the same attempt list.

**Impact and conditions:** A project that forbids local/Ollama processing can still send its prompt there if the paid provider fails and Ollama is reachable. Stock policy permits that default, so this violation requires a stricter project policy. Budget downgrade is separately rechecked by input governance.

**Recommended change:** Make fallback destinations explicit per project and authorize every final provider/model before dispatch. Fail clearly when no authorized route remains.

**Acceptance check:** Configure a project that forbids Ollama, fail the first provider, and assert no Ollama request occurs in both chat and streaming.

**Source:** [app/services/fallback_service.py:202](</D:/GenAI/TokenGuardAI/app/services/fallback_service.py:202>), [app/services/fallback_service.py:310](</D:/GenAI/TokenGuardAI/app/services/fallback_service.py:310>), [app/api/chat.py:468](</D:/GenAI/TokenGuardAI/app/api/chat.py:468>), [app/api/streaming.py:780](</D:/GenAI/TokenGuardAI/app/api/streaming.py:780>).

### R06 · P1 · Budget downgrade converts hard-limit failures into allowed requests

**Area:** Budgets. **Evidence:** All token and monetary violations share one reasons list. If downgrade is enabled and available, the service returns allowed=True without reducing tokens or reevaluating hard limits on the destination.

**Impact and conditions:** Offline reproduction allowed a 700-token request against a 100-token ceiling. The shipped downgrade route is OpenRouter to Ollama. A cheaper provider does not make an oversized prompt/output compliant.

**Recommended change:** Separate hard safety/resource ceilings from soft spend policies. After any reroute, calculate the final allowance again; block if a hard constraint remains violated.

**Acceptance check:** Cover each input/output/total-token limit with downgrade enabled, exhausted daily budget, and a destination whose limits differ.

**Source:** [app/services/budget_service.py:325](</D:/GenAI/TokenGuardAI/app/services/budget_service.py:325>), [app/services/budget_service.py:365](</D:/GenAI/TokenGuardAI/app/services/budget_service.py:365>), [app/api/chat.py:348](</D:/GenAI/TokenGuardAI/app/api/chat.py:348>).

### R07 · P1 · Project spend checks do not provide an atomic project-wide ceiling

**Area:** Budgets. **Evidence:** Daily usage is filtered by provider and status='completed'. Admission is a read/check followed later by generation and logging, with no spend/token reservation.

**Impact and conditions:** Parallel requests can each pass using the same remaining budget. Changing provider creates a separate effective allowance. Billable unsafe results, failed/partial generations and retries are not fully reflected in the ceiling. Project limits therefore cannot be treated as guaranteed spend caps.

**Recommended change:** Use a project-wide usage ledger with atomic reservations, reconciliation of actual provider usage, explicit retry/partial-output accounting, and expiry/recovery for abandoned reservations. Keep per-provider limits only as additional controls.

**Acceptance check:** Race multiple requests against the last allowance; include mixed providers, completed_unsafe results, retries, disconnects and worker crashes.

**Source:** [app/services/budget_service.py:196](</D:/GenAI/TokenGuardAI/app/services/budget_service.py:196>), [app/services/budget_service.py:198](</D:/GenAI/TokenGuardAI/app/services/budget_service.py:198>), [app/api/chat.py:322](</D:/GenAI/TokenGuardAI/app/api/chat.py:322>), [app/api/streaming.py:297](</D:/GenAI/TokenGuardAI/app/api/streaming.py:297>).

### R08 · P1 · The approved output estimate is not the enforced upstream cap

**Area:** Budgets. **Evidence:** Manual routing leaves max_tokens=None when omitted. The budget estimate substitutes 500, but the provider request still receives None. OpenAI/OpenRouter/Groq/Ollama omit their cap; Anthropic applies its separate default of 1024.

**Impact and conditions:** The provider may generate more output than the amount used for approval. This finding does not affect requests with an explicit or automatic-route cap in the same way.

**Recommended change:** Resolve a concrete effective output cap once, validate it, reserve against it, and pass exactly that value to every adapter and fallback.

**Acceptance check:** For every provider and both transports, omit max_tokens and inspect the actual upstream payload; it must match the admitted/reserved output limit.

**Source:** [app/api/chat.py:310](</D:/GenAI/TokenGuardAI/app/api/chat.py:310>), [app/api/chat.py:322](</D:/GenAI/TokenGuardAI/app/api/chat.py:322>), [app/api/chat.py:463](</D:/GenAI/TokenGuardAI/app/api/chat.py:463>), [app/api/streaming.py:288](</D:/GenAI/TokenGuardAI/app/api/streaming.py:288>), [app/providers/anthropic.py:58](</D:/GenAI/TokenGuardAI/app/providers/anthropic.py:58>).

### R09 · P1 · The supplied production host/HTTPS settings conflict with the stack

**Area:** Deployment. **Evidence:** The production template allows only the public API hostname, while health checks use 127.0.0.1 and dashboard/Prometheus use api. FORCE_HTTPS_REDIRECT is enabled. Uvicorn starts without trusting the Caddy container's forwarded scheme.

**Impact and conditions:** Internal requests are rejected or redirected to TLS on a plain HTTP API port. Caddy/dashboard wait for API health. Public HTTPS can redirect back to itself because the proxy's HTTPS scheme is not trusted. These are configuration/dataflow findings; a complete live stack was not run.

**Recommended change:** Define internal health routes/hosts and a narrow trusted-proxy boundary. Prefer enforcing public HTTPS at Caddy, with explicitly configured forwarded headers if application redirects remain.

**Acceptance check:** Start the exact production overlay from a clean image; check internal readiness, dashboard calls, Prometheus scraping and a single successful public HTTPS request without a loop.

**Source:** [.env.production.example:35](</D:/GenAI/TokenGuardAI/.env.production.example:35>), [.env.production.example:37](</D:/GenAI/TokenGuardAI/.env.production.example:37>), [docker-compose.prod.yml:65](</D:/GenAI/TokenGuardAI/docker-compose.prod.yml:65>), [Dockerfile:19](</D:/GenAI/TokenGuardAI/Dockerfile:19>), [deploy/Caddyfile:46](</D:/GenAI/TokenGuardAI/deploy/Caddyfile:46>).

### R10 · P1 · SSH deployment omits the production interpolation environment

**Area:** Deployment. **Evidence:** The deploy workflow constructs its Compose command without --env-file .env.production. Service env_file does not supply Compose's ${...} interpolation.

**Impact and conditions:** A clean documented production checkout can fail on missing required secrets before deployment. A host with exported/development values can resolve a different configuration from the intended production one.

**Recommended change:** Use the explicit production env file consistently for config rendering, pull/build, backups, migration, startup and recovery. Validate the merged configuration without printing secrets.

**Acceptance check:** Run configuration validation on a clean host containing only the documented production env file and verify resolved service endpoints/images with redacted assertions.

**Source:** [.github/workflows/deploy.yml:78](</D:/GenAI/TokenGuardAI/.github/workflows/deploy.yml:78>), [docker-compose.prod.yml:57](</D:/GenAI/TokenGuardAI/docker-compose.prod.yml:57>).

### R11 · P1 · CI's encryption key is invalid

**Area:** Quality. **Evidence:** The TOKEN_GUARD_MASTER_KEY literal in the CI workflow decodes to 41 bytes. Fernet requires a URL-safe base64 encoding of exactly 32 key bytes; local construction rejected this value.

**Impact and conditions:** Tests and services that initialize EncryptionService cannot succeed under the unpatched CI environment. Passing local tests with another key does not validate this workflow.

**Recommended change:** Generate a valid disposable Fernet key for CI and make configuration validation construct/validate the encryption service before the rest of the suite.

**Acceptance check:** Run the workflow with its real environment and an unmocked encryption/fallback constructor; prove encrypt/decrypt round-trip and full test success.

**Source:** [.github/workflows/ci.yml:80](</D:/GenAI/TokenGuardAI/.github/workflows/ci.yml:80>), [app/services/encryption_service.py:1](</D:/GenAI/TokenGuardAI/app/services/encryption_service.py:1>).

### R12 · P1 · Docker builds can include production secrets and backups

**Area:** Deployment. **Evidence:** .dockerignore excludes .env but not .env.production, backups/, dump files or Streamlit secrets. Both images copy the entire build context with COPY . .

**Impact and conditions:** A local build from a production checkout can permanently embed secrets or database dumps in image layers. This is a confirmed inclusion path, not evidence that an existing image already leaked data. Git ignore rules do not protect Docker context.

**Recommended change:** Prefer explicit source copies and comprehensive sensitive-path exclusions. Give dashboard/worker only the secrets each requires; document rotation if a secret-bearing image was actually distributed.

**Acceptance check:** Build with harmless sentinel secrets and backup files in the checkout, then inspect every image layer and confirm the sentinels are absent.

**Source:** [.dockerignore:8](</D:/GenAI/TokenGuardAI/.dockerignore:8>), [Dockerfile:15](</D:/GenAI/TokenGuardAI/Dockerfile:15>), [Dockerfile.dashboard:15](</D:/GenAI/TokenGuardAI/Dockerfile.dashboard:15>), [scripts/backup_postgres.ps1:1](</D:/GenAI/TokenGuardAI/scripts/backup_postgres.ps1:1>).

### R13 · P1 · Production guards permit disabling administrator authentication

**Area:** Identity. **Evidence:** REQUIRE_ADMIN_AUTHENTICATION is configurable, and administrator dependencies honor it. Production configuration checks debug, key presence and registration, but does not require administrator authentication to remain enabled.

**Impact and conditions:** An accidental production setting can expose shared-admin management routes without the administrator key. The secure default is true; this is a dangerous accepted configuration, not the default behavior.

**Recommended change:** Reject disabled admin authentication in production. Replace broad bypass switches with test-only dependency overrides or an explicitly isolated development mode.

**Acceptance check:** Production settings with authentication disabled must fail startup; all admin routes must require authorization under the deployed configuration.

**Source:** [app/core/config.py:101](</D:/GenAI/TokenGuardAI/app/core/config.py:101>), [app/core/config.py:172](</D:/GenAI/TokenGuardAI/app/core/config.py:172>), [app/dependencies/admin_authentication.py:1](</D:/GenAI/TokenGuardAI/app/dependencies/admin_authentication.py:1>).

### R14 · P1 · Management authorization is incomplete across the control plane

**Area:** Identity. **Evidence:** JWT permission dependencies are applied to organization routes, while project edit/key lifecycle remains shared-admin-only. Credential management, jobs and analytics accept ordinary project application keys; defined permissions for those features are not wired in.

**Impact and conditions:** An organization owner cannot complete management using JWT alone, and an application inference key has unnecessarily broad management power. The dashboard still asks for shared admin and project keys. This blocks a clean multi-tenant administration model.

**Recommended change:** Add organization-scoped JWT/RBAC management routes and scoped project keys. Separate inference, read-only analytics and credential/job administration permissions; migrate the dashboard to human identity.

**Acceptance check:** Exercise the entire signup/invite-to-project-to-key-to-inference lifecycle without a shared admin key; verify a chat-only key cannot mutate credentials or submit evaluations.

**Source:** [app/api/organizations.py:393](</D:/GenAI/TokenGuardAI/app/api/organizations.py:393>), [app/api/projects.py:159](</D:/GenAI/TokenGuardAI/app/api/projects.py:159>), [app/api/credentials.py:77](</D:/GenAI/TokenGuardAI/app/api/credentials.py:77>), [app/api/jobs.py:83](</D:/GenAI/TokenGuardAI/app/api/jobs.py:83>), [app/security/permissions.py:21](</D:/GenAI/TokenGuardAI/app/security/permissions.py:21>), [dashboard/streamlit_app.py:69](</D:/GenAI/TokenGuardAI/dashboard/streamlit_app.py:69>).

### R15 · P1 · Disabling an organization does not disable its project keys

**Area:** Identity. **Evidence:** get_current_project validates active project keys and active projects, but does not join/check the parent organization's active state.

**Impact and conditions:** If an organization is suspended through a database/administrative operation while its projects remain active, its project credentials still authenticate. A public organization-suspend endpoint does not currently exist; parent-status enforcement must accompany the completed suspension workflow.

**Recommended change:** Make organization status part of project authentication, with explicit behavior for legacy organization-less projects. Invalidate any auth cache on suspension.

**Acceptance check:** Disable only the parent organization and assert every project-key-protected operation is denied; reactivation follows documented policy.

**Source:** [app/dependencies/authentication.py:60](</D:/GenAI/TokenGuardAI/app/dependencies/authentication.py:60>).

### R16 · P2 · Exact-cache identity differs between Redis and SQL

**Area:** Cache. **Evidence:** The SQL key includes credential_id and preserves message content; the Redis key omits credential_id and strips message whitespace. Cache hits can happen before explicit credential validation.

**Impact and conditions:** Within a project, two requests with different credential identities or meaningful leading/trailing whitespace can collide in Redis. A revoked or invalid requested credential can receive a cached result. This is not evidence of cross-project leakage.

**Recommended change:** Use one canonical cache identity across tiers, preserving semantically relevant input and including credential/configuration versions where required. Validate request authorization/credential eligibility before returning a hit.

**Acceptance check:** Assert Redis and SQL key equivalence for the same request and separation for differing credential IDs and whitespace-sensitive prompts.

**Source:** [app/services/cache_service.py:23](</D:/GenAI/TokenGuardAI/app/services/cache_service.py:23>), [app/services/redis_cache_service.py:37](</D:/GenAI/TokenGuardAI/app/services/redis_cache_service.py:37>), [app/api/chat.py:650](</D:/GenAI/TokenGuardAI/app/api/chat.py:650>), [app/api/streaming.py:595](</D:/GenAI/TokenGuardAI/app/api/streaming.py:595>).

### R17 · P1 · Semantic cache ignores the conversation history

**Area:** Cache. **Evidence:** Semantic matching embeds only the latest user message and separately fingerprints system messages. Previous user/assistant turns do not participate in matching. System text normalization also lowercases content.

**Impact and conditions:** Different conversations in the same project/cache partition can receive the same answer to an identical final question despite different earlier facts. Offline extraction reproduced identical matching inputs. This is a within-project correctness/privacy-context issue, not demonstrated cross-tenant access.

**Recommended change:** Disable semantic reuse for multi-turn requests until the complete relevant context is represented and evaluated. Preserve case-sensitive instructions; version the matching scheme.

**Acceptance check:** Use conversations with different names, orders, balances or code followed by the same question; confirm no inappropriate reuse. Measure false positives on realistic holdout data.

**Source:** [app/services/semantic_cache_service.py:42](</D:/GenAI/TokenGuardAI/app/services/semantic_cache_service.py:42>), [app/services/semantic_cache_service.py:52](</D:/GenAI/TokenGuardAI/app/services/semantic_cache_service.py:52>), [app/services/semantic_cache_service.py:126](</D:/GenAI/TokenGuardAI/app/services/semantic_cache_service.py:126>).

### R18 · P1 · Cached output bypasses the current output policy

**Area:** Governance. **Evidence:** Redis, SQL-exact and semantic hit paths return before evaluate_output. Cache identities contain no output-policy/scanner version, and policy changes do not invalidate entries.

**Impact and conditions:** Content admitted under weaker settings, earlier scanners, or moderation failure can continue to be served after controls tighten. Newly generated cache entries normally passed the then-current scan; input governance still runs.

**Recommended change:** Revalidate cached output when required, or version entries by the effective output policy and invalidate incompatible versions. Include a tenant purge operation.

**Acceptance check:** Cache an answer under a weaker policy, enable a rule that forbids it, and verify all cache tiers and streaming refuse the old answer.

**Source:** [app/api/chat.py:701](</D:/GenAI/TokenGuardAI/app/api/chat.py:701>), [app/api/chat.py:796](</D:/GenAI/TokenGuardAI/app/api/chat.py:796>), [app/api/chat.py:895](</D:/GenAI/TokenGuardAI/app/api/chat.py:895>), [app/api/chat.py:1024](</D:/GenAI/TokenGuardAI/app/api/chat.py:1024>), [app/api/streaming.py:692](</D:/GenAI/TokenGuardAI/app/api/streaming.py:692>).

### R19 · P2 · Database caches have no expiration, purge lifecycle or embedding version

**Area:** Cache. **Evidence:** Redis entries have TTLs, but SQL exact and semantic lookups do not enforce expiration. The task named cleanup_expired_cache cleans jobs/health events instead. Semantic entries do not record the embedding model/version.

**Impact and conditions:** SQL can serve old answers long after Redis expiry and retain prompt/response content indefinitely. Changing embedding model/dimensions can break or invalidate comparisons. Retention and model upgrades are not controlled.

**Recommended change:** Add explicit expiry, tenant invalidation, bounded cleanup, policy/model/schema versions, and migration/rebuild rules for embeddings.

**Acceptance check:** Expire an entry across both tiers; purge a project; change embedding model/dimension; verify no stale or incompatible entry is reused.

**Source:** [app/services/cache_service.py:49](</D:/GenAI/TokenGuardAI/app/services/cache_service.py:49>), [app/services/semantic_cache_service.py:134](</D:/GenAI/TokenGuardAI/app/services/semantic_cache_service.py:134>), [app/services/semantic_cache_service.py:190](</D:/GenAI/TokenGuardAI/app/services/semantic_cache_service.py:190>), [app/workers/tasks/cleanup_tasks.py:51](</D:/GenAI/TokenGuardAI/app/workers/tasks/cleanup_tasks.py:51>).

### R20 · P2 · Concurrent cache misses can fail after paid generation

**Area:** Cache. **Evidence:** ExactResponseCache.cache_key is unique, but save_response always inserts and commits. Simultaneous misses can generate the same response key and race on insertion. Hit counts use read/modify/write.

**Impact and conditions:** One otherwise successful request can fail on a uniqueness error after a paid provider call. Client retry can spend again. Concurrent hits can also lose counter increments.

**Recommended change:** Use a conflict-safe upsert or insert-and-load strategy; cache persistence failure must not turn a completed generation into an unhandled failure. Use atomic counters or derive analytics from events.

**Acceptance check:** Race identical misses with a fake billable provider; all clients get valid responses and at most one cache row is stored. Test concurrent counter increments.

**Source:** [app/services/cache_service.py:65](</D:/GenAI/TokenGuardAI/app/services/cache_service.py:65>), [app/services/cache_service.py:93](</D:/GenAI/TokenGuardAI/app/services/cache_service.py:93>), [app/services/cache_service.py:102](</D:/GenAI/TokenGuardAI/app/services/cache_service.py:102>), [app/database/models.py:499](</D:/GenAI/TokenGuardAI/app/database/models.py:499>).

### R21 · P2 · Optional ML services initialize eagerly and depend on runtime downloads

**Area:** Deployment. **Evidence:** SemanticCacheService constructs SentenceTransformer and GovernanceService constructs PIIService before their enabled checks. Images do not package the required embedding/spaCy assets; Presidio can download a missing model.

**Impact and conditions:** Disabled features still incur heavy initialization. First requests can block/fail offline, and workers on the internal network cannot download required assets. A local machine with cached models hides this defect.

**Recommended change:** Initialize only enabled services, pin and package required model artifacts, load them before readiness, and fail startup clearly if required artifacts are missing.

**Acceptance check:** Start a fresh image with empty model caches and denied outbound downloads; enabled models work from packaged assets and disabled features do not initialize them.

**Source:** [app/services/semantic_cache_service.py:26](</D:/GenAI/TokenGuardAI/app/services/semantic_cache_service.py:26>), [app/services/semantic_cache_service.py:35](</D:/GenAI/TokenGuardAI/app/services/semantic_cache_service.py:35>), [app/services/pii_service.py:39](</D:/GenAI/TokenGuardAI/app/services/pii_service.py:39>), [app/services/governance_service.py:69](</D:/GenAI/TokenGuardAI/app/services/governance_service.py:69>), [Dockerfile:13](</D:/GenAI/TokenGuardAI/Dockerfile:13>).

### R22 · P2 · Accepted providers do not all implement streaming

**Area:** Gateway. **Evidence:** The schema accepts OpenAI, Anthropic and Groq, but those adapters inherit BaseLLMProvider.stream's NotImplementedError. Only Ollama and OpenRouter implement streaming.

**Impact and conditions:** An apparently valid streaming request can fail internally and fall back to another model/provider, also affecting health/circuit state. Adapter capability is not exposed to callers before starting SSE.

**Recommended change:** Implement the missing adapters or reject unsupported provider/transport combinations before opening a stream. Publish a tested capability matrix.

**Acceptance check:** Run a contract test for every accepted provider and transport; unsupported combinations return an explicit validation/capability response and do not trigger fallback.

**Source:** [app/providers/base.py:46](</D:/GenAI/TokenGuardAI/app/providers/base.py:46>), [app/providers/openai.py:1](</D:/GenAI/TokenGuardAI/app/providers/openai.py:1>), [app/providers/anthropic.py:1](</D:/GenAI/TokenGuardAI/app/providers/anthropic.py:1>), [app/providers/groq.py:1](</D:/GenAI/TokenGuardAI/app/providers/groq.py:1>), [app/schemas/chat.py:31](</D:/GenAI/TokenGuardAI/app/schemas/chat.py:31>).

### R23 · P1 · Stream errors and incomplete streams can be recorded as success

**Area:** Gateway. **Evidence:** The OpenRouter parser handles tokens/usage/[DONE] but not the top-level error field. The stream orchestrator treats normal iterator exhaustion as success without requiring a valid terminal/completed event.

**Impact and conditions:** An HTTP-200 stream containing an in-band error, or a stream ending without completion, can become an empty/partial successful answer and be cached. Official OpenRouter documentation explicitly describes errors in SSE data events.

**Recommended change:** Parse in-band errors, track terminal state and finish reason, and distinguish completion, truncation, failure and cancellation. Never cache incomplete results.

**Acceptance check:** Feed a 200 error-only SSE stream, an error after tokens, EOF without completion, malformed events and a valid terminal usage sequence.

**Source:** [app/providers/openrouter.py:200](</D:/GenAI/TokenGuardAI/app/providers/openrouter.py:200>), [app/providers/openrouter.py:218](</D:/GenAI/TokenGuardAI/app/providers/openrouter.py:218>), [app/api/streaming.py:819](</D:/GenAI/TokenGuardAI/app/api/streaming.py:819>), [app/api/streaming.py:832](</D:/GenAI/TokenGuardAI/app/api/streaming.py:832>).

### R24 · P2 · Circuit-breaker state lets one tenant affect other tenants

**Area:** Gateway. **Evidence:** Redis circuit-breaker keys are provider-wide. Provider exceptions, including bad credentials, can count against that common state.

**Impact and conditions:** A tenant with invalid credentials can suppress attempts for healthy credentials belonging to other projects. Backup credentials can be skipped because their provider shares the same open circuit.

**Recommended change:** Separate provider-wide transport health from credential/project failures; classify errors before updating a breaker and scope each breaker deliberately.

**Acceptance check:** Repeated 401 responses for project A must not disable valid project B credentials; a genuine provider-wide outage should still trigger controlled protection.

**Source:** [app/services/redis_circuit_breaker_service.py:45](</D:/GenAI/TokenGuardAI/app/services/redis_circuit_breaker_service.py:45>), [app/services/redis_circuit_breaker_service.py:51](</D:/GenAI/TokenGuardAI/app/services/redis_circuit_breaker_service.py:51>), [app/services/fallback_service.py:310](</D:/GenAI/TokenGuardAI/app/services/fallback_service.py:310>).

### R25 · P2 · Retry decisions depend on exception text instead of structured provider errors

**Area:** Gateway. **Evidence:** Provider adapters raise custom exceptions without consistently preserving HTTP status. RetryService first checks status_code, then uses message matching.

**Impact and conditions:** A transient 5xx response with a generic body can miss retries; an unrelated error mentioning a retry keyword can be retried. There is no consistent cross-adapter retry-after/total-deadline contract.

**Recommended change:** Define a structured provider exception with status, retryability, safe message, retry-after and provider request ID. Bound retries by a request deadline and include their usage in accounting.

**Acceptance check:** Test 400/401/429/500/502/timeouts with arbitrary bodies across all adapters, and enforce the configured maximum end-to-end duration.

**Source:** [app/services/provider_retry_service.py:43](</D:/GenAI/TokenGuardAI/app/services/provider_retry_service.py:43>), [app/providers/openrouter.py:94](</D:/GenAI/TokenGuardAI/app/providers/openrouter.py:94>), [app/providers/openai.py:1](</D:/GenAI/TokenGuardAI/app/providers/openai.py:1>).

### R26 · P2 · Unknown pricing and rough token estimates weaken admission

**Area:** Budgets. **Evidence:** Unknown model pricing returns zero with pricing_found=False. Token estimation counts regex word/symbol groups, so a very long unbroken or CJK string can be counted as roughly one token. Request content/message lists lack meaningful upper size limits.

**Impact and conditions:** Unpriced paid models can evade meaningful dollar checks; inaccurate estimates admit work far larger than expected. Oversized bodies also consume parsing, NLP and database resources before provider dispatch.

**Recommended change:** Require a known/conservative price for paid routes, canonicalize model aliases and version pricing. Use provider-aware tokenization or conservative bounds plus request-byte, message-count and content-length limits.

**Acceptance check:** Reject unsupported paid-price mappings; test long unbroken text, multilingual input, many messages and large bodies. Reconcile estimates against provider usage.

**Source:** [app/services/pricing_service.py:87](</D:/GenAI/TokenGuardAI/app/services/pricing_service.py:87>), [app/services/token_service.py:21](</D:/GenAI/TokenGuardAI/app/services/token_service.py:21>), [app/schemas/chat.py:14](</D:/GenAI/TokenGuardAI/app/schemas/chat.py:14>), [app/services/budget_service.py:325](</D:/GenAI/TokenGuardAI/app/services/budget_service.py:325>).

### R27 · P2 · Authentication throttling and timing need hardening

**Area:** Identity. **Evidence:** _client_ip trusts the first X-Forwarded-For value without checking the sender. Unknown-user login verifies a malformed placeholder Argon2 hash instead of a valid dummy hash. Redis throttling is allowed to fail open.

**Impact and conditions:** Direct/untrusted-proxy clients can vary the claimed IP to evade IP throttling. Unknown accounts take substantially less password work, enabling timing enumeration. Existing account lockout remains useful but does not replace robust IP/account controls.

**Recommended change:** Trust forwarded headers only from configured proxies; use a valid precomputed dummy hash with equal verification work; define and observe a bounded degraded-mode throttle.

**Acceptance check:** Compare known/unknown failure timing statistically; test forged headers from trusted/untrusted peers, concurrent failed attempts and Redis loss.

**Source:** [app/api/auth.py:46](</D:/GenAI/TokenGuardAI/app/api/auth.py:46>), [app/api/auth.py:62](</D:/GenAI/TokenGuardAI/app/api/auth.py:62>), [app/services/identity_service.py:169](</D:/GenAI/TokenGuardAI/app/services/identity_service.py:169>).

### R28 · P2 · Enabled moderation silently becomes safe on missing keys or errors

**Area:** Governance. **Evidence:** Input/output moderation returns safe=True when no moderation key is configured or any moderation exception occurs. The result does not distinguish checked-safe from skipped/unavailable.

**Impact and conditions:** A project relying on moderation cannot tell when enforcement was absent. This is a deliberate availability tradeoff in comments, but it is not configurable or visible enough to support a strict safety policy.

**Recommended change:** Separate allowed, blocked and unavailable decisions; validate required credentials at startup; configure fail-open/fail-closed behavior per policy and emit audit/operational events.

**Acceptance check:** Simulate missing credentials, timeout, provider rejection and successful moderation; strict projects must not receive unchecked output.

**Source:** [app/services/governance_service.py:224](</D:/GenAI/TokenGuardAI/app/services/governance_service.py:224>), [app/services/governance_service.py:239](</D:/GenAI/TokenGuardAI/app/services/governance_service.py:239>), [app/services/governance_service.py:291](</D:/GenAI/TokenGuardAI/app/services/governance_service.py:291>).

### R29 · P2 · Sensitive-content handling is not consistent across logs and caches

**Area:** Privacy. **Evidence:** Request/cache models retain prompt and response text. STORE_RAW_SECURITY_CONTENT does not govern those stores. Provider error strings flow into attempts/logs, while the JSON formatter has no general secret-redaction layer.

**Impact and conditions:** Disabling raw security-audit content is not equivalent to disabling raw request storage. Upstream errors may echo sensitive material. No actual secret disclosure was demonstrated; the retention/error data paths need a defined policy.

**Recommended change:** Define per-store retention and redaction, minimize stored payloads by default, sanitize provider errors, restrict access, and support tenant export/deletion. Keep category-only audit records where possible.

**Acceptance check:** Use sentinel secrets in prompts, outputs and mocked upstream errors, then search every persisted/logged/client-visible field; verify expiry and deletion across caches/backups.

**Source:** [app/database/models.py:407](</D:/GenAI/TokenGuardAI/app/database/models.py:407>), [app/services/cache_service.py:78](</D:/GenAI/TokenGuardAI/app/services/cache_service.py:78>), [app/observability/logging.py:1](</D:/GenAI/TokenGuardAI/app/observability/logging.py:1>), [app/services/fallback_service.py:1](</D:/GenAI/TokenGuardAI/app/services/fallback_service.py:1>).

### R30 · P2 · Job publication and cancellation do not form a reliable lifecycle

**Area:** Jobs. **Evidence:** The API commits a queued job before apply_async with no outbox/compensation. Cancellation revokes with terminate=False and marks cancelled, but the running worker checks only at startup and later writes completed. Cancellation omits completed_at.

**Impact and conditions:** A broker failure leaves an orphan queued record. A cancelled running job continues and can overwrite cancellation. Timestamp-based cleanup leaves cancelled rows indefinitely. Late-ack redelivery can rerun already-completed work.

**Recommended change:** Use an outbox or reconciler for publication, atomic terminal-state transitions, cooperative cancellation and idempotent job execution. Set terminal timestamps and enforce per-project job quotas.

**Acceptance check:** Test broker failure after commit, cancel while running, duplicate delivery, worker crash/restart and retention of every terminal state.

**Source:** [app/api/jobs.py:97](</D:/GenAI/TokenGuardAI/app/api/jobs.py:97>), [app/api/jobs.py:170](</D:/GenAI/TokenGuardAI/app/api/jobs.py:170>), [app/workers/tasks/evaluation_tasks.py:38](</D:/GenAI/TokenGuardAI/app/workers/tasks/evaluation_tasks.py:38>), [app/workers/tasks/evaluation_tasks.py:77](</D:/GenAI/TokenGuardAI/app/workers/tasks/evaluation_tasks.py:77>), [app/workers/tasks/cleanup_tasks.py:69](</D:/GenAI/TokenGuardAI/app/workers/tasks/cleanup_tasks.py:69>).

### R31 · P2 · Daily aggregation misses prior-day tail and downtime

**Area:** Analytics. **Evidence:** The scheduled usage task reads only records created since today's UTC midnight and writes only today's aggregate.

**Impact and conditions:** Requests after the last run before midnight never enter yesterday's row. A missed day cannot be recovered by the next ordinary run. This makes durable daily totals incomplete.

**Recommended change:** Use a watermark/incremental ledger or recompute a bounded recent-day window with an explicit backfill command and idempotent upserts.

**Acceptance check:** Create usage just before midnight, run after midnight, skip a scheduled interval and replay a batch; all totals must reconcile with the source ledger.

**Source:** [app/workers/tasks/usage_tasks.py:21](</D:/GenAI/TokenGuardAI/app/workers/tasks/usage_tasks.py:21>), [app/workers/tasks/usage_tasks.py:82](</D:/GenAI/TokenGuardAI/app/workers/tasks/usage_tasks.py:82>).

### R32 · P2 · Savings and reporting periods are not trustworthy

**Area:** Analytics. **Evidence:** Redis cache hits do not increment SQL cache hit_count, while savings calculations depend on those counters. USD savings are hardcoded to zero. Analytics reads retained raw logs, not ProjectUsageDaily; some panels use unfiltered retained-history totals while days filters the daily series.

**Impact and conditions:** Default Redis traffic underreports savings, paid-provider cost savings remain zero, and historical totals shrink when raw logs expire. The dashboard can show figures for different periods without making the distinction clear. Unsafe/billable outcomes also need consistent treatment.

**Recommended change:** Build analytics from a durable, reconciled usage/cache-benefit ledger; apply a common requested period or clearly label separate periods, and distinguish estimates from confirmed usage.

**Acceptance check:** Compare API/dashboard results with a known dataset spanning Redis/SQL/semantic hits, paid models, unsafe results, retention cutoff and multiple date windows.

**Source:** [app/services/analytics_service.py:128](</D:/GenAI/TokenGuardAI/app/services/analytics_service.py:128>), [app/services/analytics_service.py:299](</D:/GenAI/TokenGuardAI/app/services/analytics_service.py:299>), [app/api/chat.py:650](</D:/GenAI/TokenGuardAI/app/api/chat.py:650>), [app/api/analytics.py:63](</D:/GenAI/TokenGuardAI/app/api/analytics.py:63>), [dashboard/streamlit_app.py:365](</D:/GenAI/TokenGuardAI/dashboard/streamlit_app.py:365>).

### R33 · P1 · Container startup can install development dependencies

**Area:** Deployment. **Evidence:** Images build with uv sync --frozen --no-dev, but API/dashboard/worker/beat start with plain uv run. uv run synchronizes default groups, including dev, unless configured otherwise.

**Impact and conditions:** Prepared images can install extra packages at startup and require network access. Worker/beat have only an internal backend network, so a clean environment can fail before application startup.

**Recommended change:** Execute the already-installed virtual environment directly, or consistently configure no-sync/no-dev frozen execution. Pin build tooling and split runtime dependency groups.

**Acceptance check:** Start every image with no package-index access and a read-only dependency environment; confirm no installation attempt and no development dependencies in runtime images.

**Source:** [Dockerfile:13](</D:/GenAI/TokenGuardAI/Dockerfile:13>), [Dockerfile:19](</D:/GenAI/TokenGuardAI/Dockerfile:19>), [Dockerfile.dashboard:19](</D:/GenAI/TokenGuardAI/Dockerfile.dashboard:19>), [docker-compose.yml:83](</D:/GenAI/TokenGuardAI/docker-compose.yml:83>), [docker-compose.prod.yml:83](</D:/GenAI/TokenGuardAI/docker-compose.prod.yml:83>).

### R34 · P2 · Migration downgrade cannot drop an unnamed constraint

**Area:** Deployment. **Evidence:** The enterprise identity migration creates an unnamed foreign key and later invokes op.drop_constraint(None, ...). Metadata provides no naming convention. Offline PostgreSQL compilation raised CompileError because the constraint has no name.

**Impact and conditions:** A documented downgrade/rollback path is broken. A deployment recovery plan must not assume this operation works.

**Recommended change:** Give constraints stable names, account for databases already migrated, and test upgrades/downgrades with realistic data. Prefer forward-compatible recovery and verified backups over relying only on downgrade.

**Acceptance check:** Run upgrade from the prior release, downgrade, then re-upgrade in isolated PostgreSQL and verify schema and data invariants.

**Source:** [alembic/versions/1a75f2dabe4b_add_enterprise_identity_and_rbac.py:107](</D:/GenAI/TokenGuardAI/alembic/versions/1a75f2dabe4b_add_enterprise_identity_and_rbac.py:107>), [alembic/versions/1a75f2dabe4b_add_enterprise_identity_and_rbac.py:114](</D:/GenAI/TokenGuardAI/alembic/versions/1a75f2dabe4b_add_enterprise_identity_and_rbac.py:114>).

### R35 · P1 · Restore validation can target real data and ignore restore failure

**Area:** Recovery. **Evidence:** restore_test.ps1 accepts an arbitrary TargetDatabase and drops it without excluding the application database. It ignores pg_restore's exit status and prints selected counts/revision without comparing them with backup expectations.

**Impact and conditions:** An incorrect target can destroy the live database. A partial restore can pass the limited checks and still print success. No destructive restore was run during this review.

**Recommended change:** Require a dedicated scratch server/database with an explicit safe-name guard. Fail on every native-command error, use restore error-stop behavior, and verify schema/data expectations and application smoke checks.

**Acceptance check:** Reject the production target; inject corrupt/incompatible dumps and SQL failures; prove only a complete restore passes, on an isolated environment.

**Source:** [scripts/restore_test.ps1:25](</D:/GenAI/TokenGuardAI/scripts/restore_test.ps1:25>), [scripts/restore_test.ps1:56](</D:/GenAI/TokenGuardAI/scripts/restore_test.ps1:56>), [scripts/restore_test.ps1:60](</D:/GenAI/TokenGuardAI/scripts/restore_test.ps1:60>), [scripts/restore_test.ps1:75](</D:/GenAI/TokenGuardAI/scripts/restore_test.ps1:75>).

### R36 · P2 · Database customization and backup helpers disagree with production configuration

**Area:** Deployment. **Evidence:** Base Compose hardcodes PostgreSQL user/database to tokenguard, while production client DSNs interpolate configurable values. Standalone backup/restore helpers use bare docker compose instead of the production files/env arguments.

**Impact and conditions:** Customizing advertised database settings can break connections. Backup/restore can fail on a clean production checkout containing only .env.production, before reaching the running database.

**Recommended change:** Use one shared production Compose invocation and one source of truth for database/user names, health checks and helper commands.

**Acceptance check:** Run a clean setup with nondefault user/database names; create a backup and restore it to an isolated target through the documented helper commands.

**Source:** [docker-compose.yml:7](</D:/GenAI/TokenGuardAI/docker-compose.yml:7>), [docker-compose.prod.yml:57](</D:/GenAI/TokenGuardAI/docker-compose.prod.yml:57>), [scripts/backup_postgres.ps1:35](</D:/GenAI/TokenGuardAI/scripts/backup_postgres.ps1:35>), [scripts/restore_test.ps1:33](</D:/GenAI/TokenGuardAI/scripts/restore_test.ps1:33>).

### R37 · P2 · Readiness can stay green without the authoritative database

**Area:** Deployment. **Evidence:** /health always returns HTTP 200 and can say degraded while only one dependency is alive. Container probes check HTTP success. The PowerShell deploy script accepts degraded and only prints instructions to run the smoke test.

**Impact and conditions:** Traffic or deployment can be accepted while PostgreSQL is unavailable. The CI deployment's later smoke_test.py does check database health, so this is specifically a container/PowerShell readiness gap.

**Recommended change:** Separate process liveness from service readiness, require PostgreSQL for readiness, decide which Redis failures are tolerable, and run the same mandatory smoke gate in every deploy path.

**Acceptance check:** Stop PostgreSQL and then Redis separately; observe correct readiness, traffic behavior and deployment failure/recovery.

**Source:** [app/api/health.py:75](</D:/GenAI/TokenGuardAI/app/api/health.py:75>), [docker-compose.prod.yml:65](</D:/GenAI/TokenGuardAI/docker-compose.prod.yml:65>), [scripts/deploy_production.ps1:163](</D:/GenAI/TokenGuardAI/scripts/deploy_production.ps1:163>), [scripts/smoke_test.py:68](</D:/GenAI/TokenGuardAI/scripts/smoke_test.py:68>).

### R38 · P2 · The output-secret alert watches input PII and has no notification routing

**Area:** Observability. **Evidence:** TokenGuardOutputSecretsDetected selects pii_findings_total{action='block'}, which is incremented for input PII. Output blocks use governance_decisions_total with event_type='output'. Prometheus configuration contains no active Alertmanager notification path.

**Impact and conditions:** Real output-secret events do not fire the intended alert; input PII can falsely trigger it. Even correctly firing rules do not notify an on-call recipient with the supplied configuration.

**Recommended change:** Use a dedicated output-secret metric or correct event labels, test Prometheus rules, and connect a real notification receiver with a runbook and ownership.

**Acceptance check:** Inject input PII and output-secret events independently, assert the correct rule fires, then verify delivery and resolution to a test receiver.

**Source:** [observability/alert_rules.yml:137](</D:/GenAI/TokenGuardAI/observability/alert_rules.yml:137>), [app/api/chat.py:515](</D:/GenAI/TokenGuardAI/app/api/chat.py:515>), [app/api/chat.py:1072](</D:/GenAI/TokenGuardAI/app/api/chat.py:1072>), [observability/prometheus.yml:8](</D:/GenAI/TokenGuardAI/observability/prometheus.yml:8>).

### R39 · P2 · Metrics are exposed through the public API proxy

**Area:** Observability. **Evidence:** /metrics has no authentication, and the Caddy API reverse proxy forwards the path. Metrics include project/provider/model activity labels and operational/cost information.

**Impact and conditions:** An external caller can learn tenant activity and infrastructure details if the production route is reachable. Metrics should not share the unrestricted public application boundary.

**Recommended change:** Serve metrics on an internal listener/network or restrict the path at the proxy to authenticated/allowlisted monitoring clients. Bound label cardinality.

**Acceptance check:** Public requests to /metrics are denied while the private Prometheus scrape succeeds; confirm no raw user content appears in labels.

**Source:** [app/api/metrics.py:15](</D:/GenAI/TokenGuardAI/app/api/metrics.py:15>), [app/observability/metrics.py:1](</D:/GenAI/TokenGuardAI/app/observability/metrics.py:1>), [deploy/Caddyfile:46](</D:/GenAI/TokenGuardAI/deploy/Caddyfile:46>).

### R40 · P1 · Publishing is not gated on the exact revision passing checks

**Area:** Release. **Evidence:** Docker and release workflows trigger independently of CI/security and do not wait for those workflows. Version tags can publish without CI/security running for that exact tagged revision. Runtime image tags are not verified immutable digests.

**Impact and conditions:** A broken or vulnerable revision can be packaged/released despite good check definitions. External branch protection was not inspected and must not be assumed to close tag-publishing paths.

**Recommended change:** Promote only an immutable artifact from a successful test/security/build job, record provenance, and deploy its digest. Make release authorization depend on exact-revision results.

**Acceptance check:** Create a failing candidate/tag in a test repository/workflow and verify publishing is blocked; verify the deployed digest matches the tested artifact.

**Source:** [.github/workflows/docker.yml:1](</D:/GenAI/TokenGuardAI/.github/workflows/docker.yml:1>), [.github/workflows/release.yml:1](</D:/GenAI/TokenGuardAI/.github/workflows/release.yml:1>), [.github/workflows/ci.yml:1](</D:/GenAI/TokenGuardAI/.github/workflows/ci.yml:1>), [.github/workflows/security.yml:1](</D:/GenAI/TokenGuardAI/.github/workflows/security.yml:1>).

### R41 · P1 · Test setup can flush an unintended Redis database

**Area:** Quality. **Evidence:** The autouse fixture calls flushdb on TEST_REDIS_URL without validating an isolated test endpoint. The PostgreSQL guard only searches the entire URL for the substring tokenguard_test and echoes the full URL on failure.

**Impact and conditions:** A mistaken test environment can erase shared Redis data. The database guard can accept a URL whose username contains the test substring while the database is production, and its error may expose credentials.

**Recommended change:** Require explicitly provisioned disposable services, validate parsed database names and endpoints, use isolated key namespaces where practical, and redact connection strings. Separate unit tests from integration infrastructure.

**Acceptance check:** Point fixtures at deliberately unsafe dummy URLs and assert refusal before any network mutation; unit tests should run without Redis/PostgreSQL.

**Source:** [tests/conftest.py:89](</D:/GenAI/TokenGuardAI/tests/conftest.py:89>), [tests/conftest.py:98](</D:/GenAI/TokenGuardAI/tests/conftest.py:98>), [tests/conftest.py:111](</D:/GenAI/TokenGuardAI/tests/conftest.py:111>).

### R42 · P2 · Several exposed settings and policy fields do not control behavior

**Area:** Configuration. **Evidence:** Application searches found no effective reads for authentication_enabled, require_email_verification, policy_enforcement_enabled, metrics_path and otel_exporter_otlp_endpoint; the separate otel_exporter_otlp_traces_endpoint is used. Budget enforcement_enabled is reported but not used to skip enforcement. Some shipped governance action/blocked-term fields are not consumed.

**Impact and conditions:** Operators can believe a behavior is enabled, disabled or routed elsewhere when it is unchanged. Unused security flags and policy fields are especially misleading.

**Recommended change:** Remove unsupported settings or implement them with strict schema validation. Reject unknown policy fields and expose a safe effective-configuration summary.

**Acceptance check:** Change every documented setting in a behavior test; require an observable intended effect or a startup validation error.

**Source:** [app/core/config.py:22](</D:/GenAI/TokenGuardAI/app/core/config.py:22>), [app/core/config.py:120](</D:/GenAI/TokenGuardAI/app/core/config.py:120>), [app/core/config.py:146](</D:/GenAI/TokenGuardAI/app/core/config.py:146>), [app/services/budget_service.py:1](</D:/GenAI/TokenGuardAI/app/services/budget_service.py:1>), [app/services/model_policy_service.py:23](</D:/GenAI/TokenGuardAI/app/services/model_policy_service.py:23>).

### R43 · P2 · Project names are unique globally instead of within an organization

**Area:** Identity. **Evidence:** Project.name has a global unique constraint, even though projects are now owned by organizations.

**Impact and conditions:** Two unrelated organizations cannot both use a normal name such as Production. This creates an unnecessary cross-tenant naming dependency and complicates onboarding.

**Recommended change:** Use a composite organization/name constraint, with explicit handling for legacy projects and a safe migration.

**Acceptance check:** Create identically named projects in separate organizations; duplicates within the same organization remain rejected.

**Source:** [app/database/models.py:1225](</D:/GenAI/TokenGuardAI/app/database/models.py:1225>).

### R44 · P2 · Swagger's OAuth password flow points to a JSON endpoint

**Area:** API. **Evidence:** OAuth2PasswordBearer advertises /v1/auth/token as the token endpoint, but that alias takes a JSON LoginRequest rather than OAuth2 form fields.

**Impact and conditions:** The advertised Swagger authorization flow fails request validation, despite ordinary JSON login working.

**Recommended change:** Implement a standards-compatible token endpoint or advertise an authentication scheme that matches the actual JSON login contract.

**Acceptance check:** Use Swagger Authorize or submit the actual password-flow form and verify successful authentication plus correct error responses.

**Source:** [app/dependencies/user_authentication.py:27](</D:/GenAI/TokenGuardAI/app/dependencies/user_authentication.py:27>), [app/api/auth.py:183](</D:/GenAI/TokenGuardAI/app/api/auth.py:183>).

### R45 · P2 · ASGI cancellation can skip final usage and audit recording

**Area:** Gateway. **Evidence:** The streaming generator handles a polled is_disconnected branch, but re-raises asyncio.CancelledError without a shared finalization path.

**Impact and conditions:** A normal ASGI task cancellation can bypass request outcome, partial token/cost and audit persistence. This also worsens the budget/accounting gaps. Polling for disconnect is not a guarantee that this branch runs.

**Recommended change:** Use an idempotent finalizer for success, failure and cancellation, with bounded/shielded persistence and explicit upstream cancellation. Reconcile uncertain provider billing later.

**Acceptance check:** Cancel the consuming ASGI task mid-stream and verify exactly one terminal ledger/audit outcome with partial usage and no unsafe cached result.

**Source:** [app/api/streaming.py:811](</D:/GenAI/TokenGuardAI/app/api/streaming.py:811>), [app/api/streaming.py:899](</D:/GenAI/TokenGuardAI/app/api/streaming.py:899>).

### R46 · P2 · Retry counts and stream outcome telemetry are incomplete

**Area:** Observability. **Evidence:** Nonstream FallbackService invokes the retry service but records retry_count=0. Streaming calls adapters directly without those same-attempt retries and lacks a corresponding ProviderAttemptLog for successful attempts. The HTTP middleware histogram ends when call_next returns, before the full body; separate provider/request latency measurements do exist.

**Impact and conditions:** Retry counts and terminal/cancelled stream outcomes are incomplete. Terminal SSE failures occur after HTTP 200. Existing successful-stream provider latency and stored request latency are useful, but the HTTP histogram does not represent full-stream duration.

**Recommended change:** Record per-attempt retry/outcome/usage events and explicit time-to-first-token, full-stream duration, cancellation and terminal-error metrics; correlate them with request IDs.

**Acceptance check:** Exercise two retries on nonstream execution, successful fallback, delayed token streams and post-header failures; compare attempt logs and full/terminal stream metrics with the known fake-provider timeline.

**Source:** [app/services/fallback_service.py:1](</D:/GenAI/TokenGuardAI/app/services/fallback_service.py:1>), [app/api/streaming.py:840](</D:/GenAI/TokenGuardAI/app/api/streaming.py:840>), [app/api/streaming.py:874](</D:/GenAI/TokenGuardAI/app/api/streaming.py:874>), [app/middleware/metrics.py:25](</D:/GenAI/TokenGuardAI/app/middleware/metrics.py:25>).

### R47 · P1 · Production has no complete account provisioning path

**Area:** Identity. **Evidence:** Public registration is prohibited in production, adding an organization member requires an existing user, and bootstrap_admin creates platform admins only. Bootstrap requires BOOTSTRAP_ADMIN_PASSWORD, but production settings reject that same value during initialization.

**Impact and conditions:** A fresh production deployment cannot follow a coherent documented path to its first administrator and ordinary invited users without special environment workarounds. Account-wide deactivation, session revocation and recovery workflows are absent; organization membership removal exists.

**Recommended change:** Implement an explicit one-shot secure bootstrap mode, owner/admin-controlled invitation or user provisioning, account recovery, deactivation and session revocation. Keep bootstrap secrets out of long-running services.

**Acceptance check:** On a fresh production-configured database, provision the first admin, invite a normal user, create a project, recover a password and offboard the user without direct SQL edits or disabling production guards.

**Source:** [scripts/bootstrap_admin.py:24](</D:/GenAI/TokenGuardAI/scripts/bootstrap_admin.py:24>), [scripts/bootstrap_admin.py:35](</D:/GenAI/TokenGuardAI/scripts/bootstrap_admin.py:35>), [app/core/config.py:193](</D:/GenAI/TokenGuardAI/app/core/config.py:193>), [app/core/config.py:198](</D:/GenAI/TokenGuardAI/app/core/config.py:198>), [app/api/organizations.py:288](</D:/GenAI/TokenGuardAI/app/api/organizations.py:288>).

### R48 · P1 · The locked dependency set has unresolved security advisories

**Area:** Dependencies. **Evidence:** The 2026-10-13 pip-audit run reported 43 entries across six installed packages; deduplicating package/advisory IDs gives 35 pairs. All six installed versions match uv.lock. The spaCy model package could not be audited on PyPI.

**Impact and conditions:** These are dependency findings requiring triage, not 35 proven application exploits. Several advisories concern unused code paths; for example the cryptography finding concerns PKCS#7 rather than this app's Fernet use. A malformed JWT-header probe was rejected as AccessTokenInvalidError in the tested environment.

**Recommended change:** Update the lockfile to compatible patched versions, remove avoidable dependencies, and document reachability/mitigation for unresolved advisories. Re-run regression tests and audit the actual runtime images, not only the developer environment.

**Acceptance check:** Produce a fresh advisory report with each remaining item fixed or explicitly risk-assessed; run identity/encryption/provider regression tests after upgrades.

**Source:** [uv.lock:1](</D:/GenAI/TokenGuardAI/uv.lock:1>), [pyproject.toml:7](</D:/GenAI/TokenGuardAI/pyproject.toml:7>), [app/services/token_service_jwt.py:107](</D:/GenAI/TokenGuardAI/app/services/token_service_jwt.py:107>).


## Dependency audit and applicability

The installed versions below match the versions in `uv.lock`. The scan covers the developer environment, which includes development/transitive packages; it is not a substitute for a container-image scan. Duplicate advisory records were deduplicated by package plus advisory ID:

- **cryptography 49.0.0:** 1 distinct advisory; scanner lists 50.0.0 as a fix.
- **GitPython 3.1.56:** 15 distinct advisories; listed fixes range through 3.1.60.
- **pip 26.1.2:** 1 distinct advisory; listed fix 26.2/26.2.0.
- **PyJWT 2.13.0:** 14 distinct advisories; listed fixes include 2.14.0 and 2.15.0; one advisory has no fixed version in the scan.
- **urllib3 2.7.0:** 3 distinct advisories; listed fix 2.8.0.
- **Werkzeug 3.1.8:** 1 distinct advisory; listed fix 3.1.9.
- **en-core-web-lg 3.8.0:** could not be audited through PyPI; verify its source, integrity and update process separately.

These are scanner-reported remediation versions as of the review date, not a tested upgrade set. Resolve compatible versions and review release notes before changing the lockfile.

Do not interpret the advisory count as 35 authentication bypasses. For example, the cryptography advisory concerns PKCS#7 EnvelopedData decryption, while the application uses Fernet; no such PKCS#7 application path was identified. The PyJWT options-reuse advisory requires reuse of a mutable options dictionary across different verification modes; the reviewed access-token call supplies no such dictionary and pins a single configured algorithm. A local malformed-header probe was rejected as an invalid access token. Other advisories still need individual reachability review. [Cryptography advisory](https://github.com/advisories/GHSA-g6cj-pr64-35w5), [PyJWT options-reuse advisory](https://github.com/advisories/GHSA-gvp8-978c-rx2q).

The raw JSON report is saved as [dependency-audit.json](</C:/Users/Yogesh/.codex/visualizations/2026/10/12/01a130b3-668d-7a33-9a18-9107a832ee08/dependency-audit.json>). It preserves the scanner's descriptions, identifiers, aliases and fix versions for triage. Retain the existing scheduled security workflow, fix its findings, and make its result a release gate.

## Missing or incomplete features

### Necessary to operate the existing product safely

1. **Complete human identity lifecycle.** Secure first-admin provisioning, invitation/provisioning of ordinary users, password change/recovery, account deactivation, session listing/revoke-all, and email verification if the product claims verified email. Decide whether to implement these or integrate an established identity provider. The current collection of flags/roles does not complete that lifecycle.
2. **Scoped application keys and tenant administration.** Inference-only, analytics-read and management scopes; key expiry/rotation/revocation; per-key limits where needed; all ordinary tenant management through JWT/RBAC. Keep shared platform credentials out of normal dashboard use. Require MFA through the chosen identity provider for externally exposed privileged accounts.
3. **An authoritative usage ledger.** Reservations, final reconciliation, idempotency, retry and partial-output costs, canonical provider/model pricing, and a clearly documented project-wide enforcement window. Distinguish estimated and provider-confirmed usage.
4. **Enforceable policy lifecycle.** Validated policy schema, explicit defaults and fallback behavior, versioning, audit history, cache invalidation and a preview/test path. File-managed policies can be enough for a small internal service; a database CRUD UI is not mandatory if that operational model is deliberate.
5. **Cache lifecycle and quality controls.** TTL across tiers, project purge, context-safe semantic matching, embedding versioning, configurable exclusion of sensitive/non-deterministic requests, and measured false-reuse rates. Semantic cache should be opt-in for workloads that tolerate approximation.
6. **Reliable asynchronous jobs.** Publication recovery, idempotent execution, cooperative cancellation, status reconciliation, per-tenant quotas, bounded dataset sizes and an approved dataset directory/upload mechanism. A free-form worker filesystem path is unsuitable as an eventual untrusted-user dataset interface.
7. **Production-ready administration UX.** JWT login/organization switching, project/key/credential lifecycle, policy/budget configuration, security events, job management, session/logout behavior, and clear expired-session/authorization errors. The current Streamlit dashboard is primarily an analytics viewer requiring manually pasted keys.
8. **Privacy and retention controls.** Clear per-store retention, raw-content opt-out/redaction, tenant purge/export, backup retention, minimal service secrets and auditable access to sensitive records.
9. **Operational recovery and notification.** Actual alert delivery, off-host backup scheduling, verified restore drills, recovery objectives, secret/key rotation runbooks, mandatory deployment smoke tests and an incident owner.
10. **Traffic limits and resource admission.** HTTP-body bounds, message/token caps, concurrency limits, job admission, pool limits, upstream deadlines and load-tested capacity. Approximate token counting alone is not a memory/CPU control.

### Add only if the intended customers need them

- **Enterprise identity:** OIDC/SAML SSO, SCIM provisioning and stronger approval workflows. MFA for externally exposed privileged accounts belongs in the baseline identity plan. An external identity provider may provide several of these with less custom authentication code.
- **Gateway compatibility:** model discovery, tool/function calls, structured outputs, multimodal messages, SDK-compatible request/response formats and documented finish reasons. Current messages are text with system/user/assistant roles and responses use a custom schema; do not claim general OpenAI compatibility without contract tests.
- **Financial/product features:** budget alerts/webhooks, billing reconciliation, invoices/subscriptions/quotas and usage export, if this becomes a hosted commercial service. These are not required for a private internal gateway.
- **Larger deployments:** indexed vector search, incremental aggregates, higher availability and multiple API/worker replicas after realistic load results justify them. Kubernetes, a microservice split and a new frontend framework are not prerequisites for correcting the current design.
- **Advanced governance:** approval-based changes, data residency controls, tenant-specific detectors and customer-managed encryption keys when requirements justify their complexity.

Prompt-injection heuristics are useful signals, not a proof that arbitrary LLM behavior is safe. Keep explicit model/tool permissions and data boundaries outside model-generated content. Existing expected-failure tests should remain visible as limitations, not be silently counted as solved defenses.

## Unwanted, misleading or unnecessarily expensive parts

These are cleanup candidates to assess and remove deliberately; no files were deleted in this review.

- **Empty modules:** `app/api/admin.py` and `app/schemas/admin.py` add no behavior.
- **Unused circuit-breaker dependency:** `pybreaker` has no application import; the project uses its custom Redis implementation. Remove it if no intended consumer remains.
- **Development tools in runtime:** `pytest` is a normal project dependency, and plain `uv run` can bring the entire dev group into containers. Separate API, dashboard, evaluation/ML and development needs.
- **Obsolete migration helper:** `scripts/migrate_phase12.py` uses an older SQLite/manual-alter workflow and describes startup behavior that conflicts with Alembic. Remove or clearly archive it after verifying no supported user relies on it.
- **Dormant governance artifacts:** `ProjectGovernancePolicy` and governance update/response schemas are not used by effective policy evaluation. Either wire them into a real lifecycle or remove premature abstractions.
- **Disconnected daily aggregate:** `ProjectUsageDaily` is written by a recurring task but not read by the analytics API. Connect it to durable reporting or remove the recurring cost until it serves a defined purpose.
- **Configuration that does nothing:** the settings and JSON policy fields in R42 should be implemented or removed. The hardcoded project `1` policy override is sample data that should not silently define a real production tenant's behavior.
- **Duplicated orchestration:** chat and streaming handlers are about 1,329 and 1,256 lines respectively. Their duplicated authorization, admission, cache, fallback and accounting logic is already producing inconsistent behavior. Extract one shared request pipeline and retain transport-specific output handling.
- **Repeated expensive setup:** policy/pricing file reads, CPU/NLP work and service construction occur in request paths. Validate/cache immutable configuration at startup and initialize models deliberately.
- **Repeated network-client creation:** per-call clients lose opportunities for connection reuse. Use bounded lifecycle-managed clients, measure contention and close them correctly.
- **Broad container copies and environment files:** dashboard and worker images receive more code/dependencies/secrets than needed. Narrow both the build context and service environment.
- **Misleading docs:** the administrator-to-JWT migration describes the shared key as a break-glass mechanism although key management/dashboard still need it; the README's API-only master-key claim conflicts with shared env files. CONTRIBUTING references a missing `tests/load/locustfile.py`; the actual file is `load_tests/locustfile.py`.
- **Misleading cleanup name:** `cleanup_expired_cache` does not expire the database caches.
- **Loose evaluation labels:** `bool(raw_example["should_reuse"])` treats the string `"false"` as true. Enforce a strict dataset schema and version evaluation inputs before relying on reported precision.
- **Generated local artifacts:** caches, coverage files, model downloads and virtual environments are normal developer artifacts when ignored. Do not label them application defects or delete them blindly; keep them out of release images and commits.

## Architecture and performance assessment

The current stack is reasonable for an internal gateway: FastAPI for requests, PostgreSQL for durable identity/configuration/usage, Redis for ephemeral acceleration/admission, Celery for expensive jobs, and a modest administration UI. Rewriting the whole stack would delay the needed fixes.

The central refactor should establish one request context and one pipeline:

1. Authenticate the principal and tenant; validate body size, schema and provider capability.
2. Resolve a concrete policy/configuration version and an authorized provider/model route.
3. Calculate a real output cap, validate hard limits and atomically reserve usage.
4. Apply input governance; calculate one canonical cache identity.
5. Check only eligible/current cache entries and apply required output policy.
6. Execute only authorized provider attempts with structured errors and a total deadline.
7. Finalize output safety, accounting, request status, audit and cache persistence consistently.
8. Settle or reconcile the reservation exactly once, including cancellation/failure.

Use a transaction/outbox boundary for durable side effects. Cache failure should not discard a successful paid response. A shared finalizer is more valuable than adding additional independent logging calls.

Several `async def` handlers call synchronous SQLAlchemy/Redis and CPU-heavy NLP directly. FastAPI does not automatically move ordinary synchronous helper calls inside an async function to its thread pool. Measure event-loop delay and concurrent tail latency, then move blocking work to suitable execution contexts or async clients. Do not infer throughput from unit-test timings. [FastAPI concurrency guidance](https://fastapi.tiangolo.com/async/).

Also address SQL semantic candidate scans, lost hit-count updates, repeated aggregate scans, unbounded tenant input, model memory sharing, database pool sizing and outbound connection pooling. Set explicit memory/CPU/process/log limits and a graceful shutdown budget before a load test. Fixed container names and migration-on-every-API-start complicate replication; run migrations as a controlled deployment job and allow one beat scheduler.

## Remediation order and release gates

### Phase 1 — Establish safe boundaries and trustworthy validation

Fix R01–R08, R13–R18, R23, R35, R41 and R47 where the corresponding capability will be enabled. Repair CI's Fernet configuration and triage/update dependencies. Add focused reproductions for the defects, not tests that merely restate the implementation.

A minimal internal pilot can reduce scope: disable public evaluation, multi-turn semantic caching, unsupported streaming and unrestricted fallback until corrected. State those limitations in the product configuration and documentation.

**Exit gate:** authorization/ownership/replay tests pass; strict output policy releases no unsafe stream bytes; hard budget tests pass under concurrency; production test infrastructure cannot target shared data.

### Phase 2 — Make the current feature set coherent

Build the shared request/admission/finalization pipeline, structured provider errors and capability validation. Add the usage ledger, cache lifecycle, reliable job state machine and consistent analytics. Complete the chosen identity/dashboard workflow and data retention controls.

**Exit gate:** representative API workflows succeed without shared platform keys; every request/job has one terminal outcome; usage/savings reconcile across retries, caches, failures and day boundaries.

### Phase 3 — Prove deployment and recovery

Fix production host/proxy/env/runtime-dependency issues, model packaging, readiness, database customization, migration recovery and backup helpers. Gate image promotion on exact-revision checks. Add container hardening, resource limits, actual notification delivery and off-host backups.

**Exit gate:** a clean host starts the exact production images without runtime downloads; public HTTPS/internal probes work; a failed dependency gives the expected readiness result; a verified isolated restore meets defined recovery objectives.

### Phase 4 — Validate at realistic traffic before expanding features

Run concurrency/soak tests with representative prompt sizes, multi-tenant traffic, cache distributions and provider latency. Include cache stampedes, Redis/database interruption, provider failures, rolling deployment, task cancellation and worker redelivery. Measure p50/p95/p99 latency, time-to-first-token, full-stream duration, error rate, memory, queue delay, database growth and estimated-versus-billed cost.

**Exit gate:** agreed SLOs are met over a sustained period, alert delivery is proven, and on-call/recovery procedures are exercised. Set concrete thresholds from expected traffic and business needs; inventing arbitrary performance targets now would create false confidence.

Do not estimate a calendar completion date until deployment target, launch scope, traffic, team capacity and identity/billing requirements are fixed. This is multiple coordinated engineering changes, not a cosmetic cleanup.

## Reference notes for operational conclusions

- Compose interpolation and service `env_file` have different roles; use an explicit production interpolation source. [Docker Compose interpolation](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/).
- `uv run` synchronizes its project environment, and development dependencies are included by default unless configured otherwise. [uv project syncing](https://docs.astral.sh/uv/concepts/projects/sync/).
- Proxy scheme handling depends on trusted forwarded sources. [Uvicorn settings](https://www.uvicorn.org/settings/).
- OpenRouter can send errors inside an already-started HTTP-200 SSE response. [OpenRouter streaming errors](https://openrouter.ai/docs/api_reference/streaming).

## Review artifacts and limitations

The offline reproduction harness is [review_checks.py](</C:/Users/Yogesh/.codex/visualizations/2026/10/12/01a130b3-668d-7a33-9a18-9107a832ee08/review_checks.py>). It deliberately uses local/in-memory paths and extracted functions where full application imports would initialize heavy models. It is evidence for the stated checks, not an end-to-end security test.

Full-run logs and coverage are retained in the [review artifact folder](</C:/Users/Yogesh/.codex/visualizations/2026/10/12/01a130b3-668d-7a33-9a18-9107a832ee08>). The integration run used ports 55433 and 56379, with all database, Redis and Celery URLs overridden to disposable services. Both label-verified disposable containers were removed after the run; existing application services were untouched.

The repository was clean at the reviewed revision before this report was added. Only review artifacts are intended changes. Environment-specific failures and incomplete checks are documented; they must not be represented as application test failures or as passing production validation.

