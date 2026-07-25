# Performance, load, stress, and soak testing

Load scenarios live in `load_tests/locustfile.py` and use
[Locust](https://locust.io/). Locust is **not** part of the GitHub Actions
workflow: it is a manual or scheduled job against a dedicated environment,
because chat traffic costs money and provider latency dominates the numbers.

## Prerequisites

Create a throwaway project, issue it an API key, and point the load test at a
local provider so the run does not bill a hosted provider:

```powershell
docker compose up -d
$env:LOAD_TEST_PROJECT_API_KEY = "tg_live_your_test_project_key"
$env:LOAD_TEST_PROVIDER = "ollama"
$env:LOAD_TEST_MODEL = "llama3.2"

# Optional: exercise the authenticated read paths as well.
$env:LOAD_TEST_ACCESS_TOKEN = "eyJ..."   # from POST /v1/auth/login
```

Interactive run (open http://127.0.0.1:8089 and start with 10 users at 2
users/second — never start with hundreds):

```powershell
uv run locust -f load_tests/locustfile.py --host http://127.0.0.1:8000
```

Headless run with CSV reports (`load-results_stats.csv`,
`load-results_failures.csv`, and so on):

```powershell
uv run locust -f load_tests/locustfile.py `
  --host http://127.0.0.1:8000 `
  --headless --users 25 --spawn-rate 5 --run-time 5m `
  --csv load-results
```

## Scenarios

| User class          | Traffic                                            | Weight | Intent |
|---------------------|----------------------------------------------------|--------|--------|
| `TokenGuardUser`    | `POST /v1/chat/completions`, `GET /v1/analytics/summary`, `GET /health` | 5 | Realistic project traffic, mixing generated and cache-warm prompts |
| `HealthUser`        | `GET /health`                                      | 2      | Load-balancer liveness traffic |
| `AuthAbuseUser`     | `POST /v1/auth/login` with bad credentials         | 1      | Credential stuffing: expect 401/429, never 200 and never 5xx |
| `AuthenticatedUser` | `GET /v1/auth/me`, `GET /v1/organizations`         | 1      | Authenticated read latency (skipped without a token) |

`TokenGuardUser` reports generated and cache-warm chat calls under separate
Locust names so the cached path can be measured on its own. Rate-limited (429)
and governance-blocked (422) chat responses are counted as successes: they are
the gateway working as designed. Any 5xx is a failure.

## Targets

Starting engineering targets for a single-node development machine, not formal
SLOs. Re-measure on the production topology before publishing any SLO.

| Signal                                       | Target |
|----------------------------------------------|--------|
| `GET /health` p95                            | < 100 ms |
| Cached chat completion p95                   | < 250 ms |
| Generated chat completion p95                | Provider-dependent; report separately |
| `GET /v1/analytics/summary` p95              | < 1 s |
| `GET /v1/auth/me` p95                        | < 100 ms |
| HTTP error rate (excluding intentional 429)  | < 1% |
| Database connection errors                   | 0 |
| Cross-project data leakage                   | 0 |
| Uncaught server errors (5xx)                 | 0 |

Provider generation latency must be reported separately from gateway overhead:

```text
gateway_overhead_ms = total_request_latency_ms - provider_latency_ms
```

Every chat response carries both numbers (`performance.latency_ms` and the
provider latency inside `reliability`), and the same values are exported as
Prometheus histograms. Targets: under 100–200 ms of gateway overhead on a
cached request, under 250 ms on a generated one.

## Observability during a run

- Prometheus `/metrics`: HTTP latency histograms, cache hit ratio, provider
  attempt counters
- Redis: CPU, memory, evicted keys, timeouts
- Postgres: active connections against `pool_size + max_overflow`
- Auth rate-limit 429 counts
- Application logs: must contain no prompts, tokens, or API keys

## Stress test

Raise the user count in steps and record the numbers at each step:

```text
25 users -> 50 users -> 100 users -> 200 users
```

Stop climbing at the first of these, and record it as the documented failure
point:

- Error rate rises sharply
- Postgres connection pool exhaustion
- Redis timeouts
- CPU pinned above 90%
- Memory grows monotonically
- p95 latency becomes unacceptable
- Celery queue backlog grows without recovering

Do not stress test against a hosted provider account without an explicit spend
cap.

## Soak test

```powershell
uv run locust -f load_tests/locustfile.py `
  --host http://127.0.0.1:8000 `
  --headless --users 20 --spawn-rate 2 --run-time 2h `
  --csv soak-results
```

Watch for resource leaks rather than latency: RSS growth, Postgres/Redis
connection counts that never return to baseline, file-descriptor growth,
Celery backlog, unbounded log growth, and OpenTelemetry exporter failures.

## Recording results

Keep the CSV output plus a short note of the topology (host, provider, worker
count) alongside each run. A target without a recorded measurement and
environment is not a result.
