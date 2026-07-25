from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
)

HTTP_REQUESTS_TOTAL = Counter(
    "tokenguard_http_requests_total",
    "Total TokenGuard HTTP requests.",
    [
        "method",
        "path",
        "status_code",
    ],
)


HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "tokenguard_http_request_duration_seconds",
    "HTTP request duration in seconds.",
    [
        "method",
        "path",
    ],
)


CHAT_REQUESTS_TOTAL = Counter(
    "tokenguard_chat_requests_total",
    "Total chat completion requests.",
    [
        "project_id",
        "provider",
        "model",
        "status",
    ],
)


PROVIDER_ATTEMPTS_TOTAL = Counter(
    "tokenguard_provider_attempts_total",
    "Provider request attempts.",
    [
        "provider",
        "model",
        "result",
        "error_type",
    ],
)


PROVIDER_LATENCY_SECONDS = Histogram(
    "tokenguard_provider_latency_seconds",
    "Provider request latency.",
    [
        "provider",
        "model",
    ],
)


TOKENS_TOTAL = Counter(
    "tokenguard_tokens_total",
    "LLM tokens processed.",
    [
        "project_id",
        "provider",
        "model",
        "token_type",
    ],
)


ESTIMATED_COST_USD_TOTAL = Counter(
    "tokenguard_estimated_cost_usd_total",
    "Estimated LLM cost in USD.",
    [
        "project_id",
        "provider",
        "model",
    ],
)


CACHE_OPERATIONS_TOTAL = Counter(
    "tokenguard_cache_operations_total",
    "Cache lookup outcomes.",
    [
        "cache_type",
        "result",
    ],
)


FALLBACKS_TOTAL = Counter(
    "tokenguard_fallbacks_total",
    "Provider fallback events.",
    [
        "original_provider",
        "final_provider",
        "reason",
    ],
)


RATE_LIMIT_REJECTIONS_TOTAL = Counter(
    "tokenguard_rate_limit_rejections_total",
    "Rate-limit rejections.",
    [
        "project_id",
        "provider",
    ],
)


BUDGET_DECISIONS_TOTAL = Counter(
    "tokenguard_budget_decisions_total",
    "Budget decisions.",
    [
        "project_id",
        "action",
    ],
)


CIRCUIT_BREAKER_STATE = Gauge(
    "tokenguard_circuit_breaker_state",
    "Circuit state: 0 closed, 1 open, 0.5 half-open.",
    [
        "provider",
    ],
)


PROVIDER_HEALTH_SCORE = Gauge(
    "tokenguard_provider_health_score",
    "Provider health score from 0 to 100.",
    [
        "provider",
        "model",
    ],
)


GOVERNANCE_DECISIONS_TOTAL = Counter(
    "tokenguard_governance_decisions_total",
    "Governance decisions.",
    [
        "project_id",
        "action",
        "event_type",
    ],
)


PII_FINDINGS_TOTAL = Counter(
    "tokenguard_pii_findings_total",
    "Sensitive-data findings.",
    [
        "entity_type",
        "action",
    ],
)


PROMPT_INJECTION_FINDINGS_TOTAL = Counter(
    "tokenguard_prompt_injection_findings_total",
    "Prompt-injection findings.",
    [
        "category",
        "action",
    ],
)
