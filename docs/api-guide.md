# API guide

Every example uses PowerShell. The interactive schema is at
`http://127.0.0.1:8000/docs` in development; it is disabled in production
unless `ENABLE_DOCS=true`.

Base URL in the examples:

```powershell
$baseUrl = "http://127.0.0.1:8000"
```

## Authentication

TokenGuard has two credential types, used for different purposes.

| Credential | Header | Used for |
|---|---|---|
| Project API key | `X-TokenGuard-Key: tg_live_...` | Sending traffic through the gateway: chat, streaming, analytics, jobs |
| User access token | `Authorization: Bearer <jwt>` | Administering organizations, projects, keys, and credentials |

A project key identifies a project, not a person. It is hashed with SHA-256
before storage, so the plaintext is shown exactly once at creation. Do not
prefix it with `Bearer`.

### Obtaining an access token

```powershell
$login = Invoke-RestMethod `
    -Uri "$baseUrl/v1/auth/login" `
    -Method Post `
    -ContentType "application/json" `
    -Body (@{
        email = "admin@example.com"
        password = "your-password"
    } | ConvertTo-Json)

$bearer = @{ Authorization = "Bearer $($login.access_token)" }
```

Access tokens expire after `ACCESS_TOKEN_EXPIRE_MINUTES` (15 by default).
Exchange the refresh token for a new pair:

```powershell
$refreshed = Invoke-RestMethod `
    -Uri "$baseUrl/v1/auth/refresh" `
    -Method Post `
    -ContentType "application/json" `
    -Body (@{ refresh_token = $login.refresh_token } | ConvertTo-Json)
```

Refresh tokens rotate on every use. Presenting a refresh token twice revokes
the whole chain, on the assumption that the token was stolen, so store only
the newest one.

### Creating a project key

```powershell
$key = Invoke-RestMethod `
    -Uri "$baseUrl/v1/projects/1/api-keys" `
    -Method Post `
    -Headers $bearer `
    -ContentType "application/json" `
    -Body (@{ name = "Production backend" } | ConvertTo-Json)

$key.api_key   # tg_live_... shown once
```

```powershell
$projectHeaders = @{ "X-TokenGuard-Key" = $key.api_key }
```

## Chat completions

`POST /v1/chat/completions`

```powershell
$body = @{
    provider = "ollama"
    model = "llama3.2"
    messages = @(
        @{ role = "user"; content = "Explain semantic caching." }
    )
    temperature = 0.3
    max_tokens = 200
    use_cache = $true
    use_semantic_cache = $true
    auto_route = $true
    routing_preference = "balanced"
} | ConvertTo-Json -Depth 6

$response = Invoke-RestMethod `
    -Uri "$baseUrl/v1/chat/completions" `
    -Method Post `
    -Headers $projectHeaders `
    -ContentType "application/json" `
    -Body $body
```

### Request fields

| Field | Type | Default | Notes |
|---|---|---|---|
| `provider` | `ollama`, `openai`, `anthropic`, `openrouter`, `groq` | `ollama` | Ignored when `auto_route` is true and routing picks another provider |
| `credential_id` | int | null | Use a specific stored credential. Defaults to the project's default credential for that provider |
| `model` | string | null | Falls back to the provider's configured default model |
| `messages` | array | required | At least one message; `role` is `system`, `user`, or `assistant` |
| `temperature` | float | `0.7` | 0.0–2.0 |
| `max_tokens` | int | null | 1–32000. May be lowered by budget enforcement |
| `use_cache` | bool | `true` | Exact-match cache |
| `use_semantic_cache` | bool | `true` | Embedding-similarity cache |
| `auto_route` | bool | `false` | Let TokenGuard pick the provider and model |
| `routing_preference` | `cost`, `balanced`, `quality` | `balanced` | Only meaningful with `auto_route` |
| `maximum_request_cost_usd` | float | null | Per-request cost ceiling, on top of the project budget |
| `maximum_total_tokens` | int | null | Per-request token ceiling |
| `allow_budget_downgrade` | bool | `true` | Permit routing to a cheaper model instead of rejecting when over budget |

### Response

Alongside `content`, the response carries the reasoning behind every decision
the gateway made. This is the part that makes the gateway auditable, so it is
worth logging in full on the client side.

```jsonc
{
  "request_id": "0f0a...",
  "provider": "ollama",
  "model": "llama3.2",
  "content": "Semantic caching reuses a response when a new prompt ...",
  "latency_ms": 812.4,

  "usage": {
    "input_tokens": 24,
    "output_tokens": 96,
    "total_tokens": 120
  },

  "cost": {
    "input_cost_usd": 0.0,
    "output_cost_usd": 0.0,
    "total_cost_usd": 0.0,
    "pricing_found": true          // false means the model is missing from config/model_pricing.json
  },

  "optimization": {
    "cache_enabled": true,
    "cache_hit": false,
    "cache_type": "none",          // none | exact | semantic
    "semantic_similarity_score": null,
    "tokens_avoided": 0,
    "cost_avoided_usd": 0.0,
    "original_latency_ms": null,   // latency of the request that populated the cache
    "cache_lookup_latency_ms": 4.1
  },

  "routing": {
    "automatic_routing_used": true,
    "tier": "simple",              // manual | simple | balanced | advanced
    "provider": "ollama",
    "model": "llama3.2",
    "complexity_score": 18,
    "confidence": 0.82,
    "reasons": ["Short prompt with simple-task indicators."],
    "original_provider": "ollama",
    "original_model": null,
    "fallback": {
      "fallback_used": false,
      "attempts_made": 1,
      "final_credential_id": 3,
      "credential_source": "project_default"
    }
  },

  "budget": {
    "enforcement_enabled": true,
    "allowed": true,
    "action": "allow",             // allow | downgrade | reject
    "original_provider": "ollama",
    "original_model": "llama3.2",
    "final_provider": "ollama",
    "final_model": "llama3.2",
    "estimated_input_tokens": 24,
    "requested_output_tokens": 200,
    "estimated_total_tokens": 224,
    "estimated_cost_usd": 0.0,
    "daily_tokens_used": 18420,
    "daily_token_budget": 200000,
    "daily_cost_used_usd": 0.42,
    "daily_cost_budget_usd": 2.0,
    "reasons": []
  },

  "reliability": {
    "correlation_id": "b31c...",   // also returned as the X-Correlation-ID header
    "provider_attempts": 1,
    "retry_count": 0,
    "circuit_breaker_state": "closed",
    "provider_health_score": 98.5,
    "fallback_used": false
  },

  "governance": {
    "allowed": true,
    "action": "allow",             // allow | redact | review | block
    "original_provider": "ollama",
    "original_model": "llama3.2",
    "sanitized": false,
    "sanitized_messages_count": 0,
    "prompt_injection_score": 0,
    "prompt_injection_findings": [],
    "sensitive_data_findings": [],
    "moderation_findings": [],
    "reasons": []
  }
}
```

Reading the interesting cases:

- **Cache hit.** `optimization.cache_hit` is true, `cache_type` is `exact` or
  `semantic`, and `tokens_avoided` / `cost_avoided_usd` quantify the saving.
  No provider was called.
- **Budget downgrade.** `budget.action` is `downgrade` and
  `budget.final_model` differs from `original_model`. The request succeeded on
  a cheaper model.
- **Fallback.** `reliability.fallback_used` is true and
  `routing.fallback.attempts_made` is above 1. The primary provider failed and
  a secondary served the request.
- **Redaction.** `governance.action` is `redact` and `sanitized` is true. PII
  was removed from the prompt before it left the gateway;
  `sensitive_data_findings` lists the entity types, never the values.

## Streaming

`POST /v1/chat/completions/stream` takes the same request body and returns
`text/event-stream`. The response carries an `X-Correlation-ID` header.

Event sequence:

| Event | Payload | Notes |
|---|---|---|
| `metadata` | `request_id`, `correlation_id`, `project_id`, `provider`, `model` | Always first. The SSE `id` field is the request ID |
| `token` | `{"type":"token","content":"..."}` | Zero or more. A cache hit emits the whole response as one token event |
| `usage` | input/output/total tokens, `estimated_cost_usd` | Once, after generation |
| `completed` | final latency and optimization details | Terminal event on success |
| `error` | `message`, and `code` where available | Terminal event on failure |

Comment lines (`: heartbeat`) arrive every `STREAM_HEARTBEAT_SECONDS` to keep
intermediaries from closing an idle connection. Ignore them.

```powershell
curl.exe -N -X POST "$baseUrl/v1/chat/completions/stream" `
    -H "X-TokenGuard-Key: $($key.api_key)" `
    -H "Content-Type: application/json" `
    -d '{\"provider\":\"ollama\",\"messages\":[{\"role\":\"user\",\"content\":\"Count to five.\"}]}'
```

A governance block or budget rejection is delivered as an HTTP error before
the stream opens, not as an `error` event.

## Analytics

`GET /v1/analytics/summary` is scoped to the project that owns the key.

```powershell
Invoke-RestMethod `
    -Uri "$baseUrl/v1/analytics/summary" `
    -Headers $projectHeaders
```

## Provider credentials

Credentials are encrypted with Fernet before storage and are never returned in
plaintext; responses show a masked form.

```powershell
# Store a credential
Invoke-RestMethod `
    -Uri "$baseUrl/v1/credentials" `
    -Method Post `
    -Headers $projectHeaders `
    -ContentType "application/json" `
    -Body (@{
        provider = "openrouter"
        name = "Primary OpenRouter key"
        api_key = "sk-or-v1-..."
        is_default = $true
    } | ConvertTo-Json)

# Verify it reaches the provider
Invoke-RestMethod `
    -Uri "$baseUrl/v1/credentials/1/test" `
    -Method Post `
    -Headers $projectHeaders
```

`POST /v1/credentials/{id}/rotate` replaces the secret while keeping the same
credential ID, so nothing referencing that ID needs updating.

## Background jobs

Long-running work returns immediately with a job ID.

```powershell
$job = Invoke-RestMethod `
    -Uri "$baseUrl/v1/jobs/evaluations/semantic-cache" `
    -Method Post `
    -Headers $projectHeaders `
    -ContentType "application/json" `
    -Body (@{ dataset_path = "evaluation/datasets/semantic_cache.json" } | ConvertTo-Json)

Invoke-RestMethod `
    -Uri "$baseUrl/v1/jobs/$($job.job_id)" `
    -Headers $projectHeaders
```

`status` moves through `queued`, `running`, and then `completed`, `failed`, or
`cancelled`. A job stays `queued` when no Celery worker is running.

## Errors

FastAPI's shape is used throughout: a `detail` field that is either a string
or an object.

```json
{
  "detail": {
    "message": "Model access denied.",
    "reason": "Model 'gpt-5' is not in the project's allowed list."
  }
}
```

| Status | Meaning | What to do |
|---|---|---|
| 400 | Malformed request | Fix the body; the detail names the field |
| 401 | Missing, invalid, or expired credential | Check the header name and that the key is active and unexpired |
| 403 | Authenticated but not permitted, or blocked by governance or model policy | Inspect `detail.reason` |
| 404 | Not found, or belongs to another tenant | Cross-tenant access returns 404 rather than 403, so IDs cannot be enumerated |
| 409 | Conflict, e.g. a duplicate project name or slug | Choose another name |
| 422 | Schema validation failed | Compare against the field table above |
| 429 | Rate limit or budget exceeded | Honour `Retry-After` and back off |
| 503 | A dependency is unavailable, or every provider attempt failed | Check `/health` and retry with backoff |

429 and 503 responses include a `Retry-After` header in seconds. Treat 429 as
routine: it means enforcement is working.

Every response carries `X-Correlation-ID`. Include it when reporting a problem;
it ties together the request log, audit events, and traces.
