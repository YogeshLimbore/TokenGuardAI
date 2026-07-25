"""
Locust load and stress scenarios for TokenGuard AI.

The API must already be running (``docker compose up -d`` or
``uv run uvicorn app.main:app``). Point Locust at it and supply a project
API key:

    $env:LOAD_TEST_PROJECT_API_KEY="tg_live_your_test_project_key"

    uv run locust -f load_tests/locustfile.py --host http://127.0.0.1:8000

Headless, with CSV reports:

    uv run locust -f load_tests/locustfile.py `
      --host http://127.0.0.1:8000 `
      --headless --users 25 --spawn-rate 5 --run-time 5m `
      --csv load-results

Baseline targets and measured results live in docs/performance.md.

Cost warning: chat traffic reaches a real provider. Keep ``LOAD_TEST_PROVIDER``
on ``ollama`` (local) unless you have explicit spend controls in place.
"""

from __future__ import annotations

import os
import random

from locust import HttpUser, between, task

PROJECT_API_KEY = os.environ.get("LOAD_TEST_PROJECT_API_KEY", "").strip()
ACCESS_TOKEN = os.environ.get("LOAD_TEST_ACCESS_TOKEN", "").strip()

PROVIDER = os.environ.get("LOAD_TEST_PROVIDER", "ollama").strip()
MODEL = os.environ.get("LOAD_TEST_MODEL", "llama3.2").strip()

PROMPTS = (
    "Explain caching in one sentence.",
    "What is an API gateway?",
    "Explain rate limiting briefly.",
    "Describe semantic caching.",
)

# A prompt that repeats often enough to exercise the cache-hit path.
CACHE_WARM_PROMPT = "What does TokenGuard do?"


class TokenGuardUser(HttpUser):
    """Mixed chat, analytics, and health traffic for a single project."""

    weight = 5
    wait_time = between(0.5, 2.0)

    def on_start(self) -> None:
        if not PROJECT_API_KEY:
            raise RuntimeError("LOAD_TEST_PROJECT_API_KEY is required.")

        self.client.headers.update(
            {
                "X-TokenGuard-Key": PROJECT_API_KEY,
                "Content-Type": "application/json",
            }
        )

    def _chat(self, prompt: str, name: str) -> None:
        with self.client.post(
            "/v1/chat/completions",
            json={
                "provider": PROVIDER,
                "credential_id": None,
                "model": MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens": 100,
                "use_cache": True,
                "use_semantic_cache": True,
                "auto_route": False,
                "routing_preference": "balanced",
                "allow_budget_downgrade": True,
            },
            name=name,
            catch_response=True,
        ) as response:
            # 429 means the rate limiter is doing its job, and 422 means a
            # governance policy blocked the request. Neither is a failure
            # of the gateway under load; 5xx is.
            if response.status_code in {422, 429}:
                response.success()
            elif response.status_code != 200:
                response.failure(f"HTTP {response.status_code}")

    @task(4)
    def chat_completion(self) -> None:
        self._chat(
            random.choice(PROMPTS),
            "POST /v1/chat/completions (generated)",
        )

    @task(3)
    def cached_chat_completion(self) -> None:
        self._chat(
            CACHE_WARM_PROMPT,
            "POST /v1/chat/completions (cache-warm)",
        )

    @task(2)
    def analytics(self) -> None:
        self.client.get(
            "/v1/analytics/summary",
            name="GET /v1/analytics/summary",
        )

    @task(1)
    def health(self) -> None:
        self.client.get("/health", name="GET /health")


class HealthUser(HttpUser):
    """Unauthenticated liveness traffic, as a load balancer would send."""

    weight = 2
    wait_time = between(0.1, 0.5)

    @task
    def health(self) -> None:
        self.client.get("/health", name="GET /health")


class AuthAbuseUser(HttpUser):
    """
    Credential stuffing against /v1/auth/login.

    Expectation under load: 401 or 429 responses, never a 5xx, and never a
    successful login.
    """

    weight = 1
    wait_time = between(0.2, 1.0)

    @task
    def failed_login(self) -> None:
        with self.client.post(
            "/v1/auth/login",
            json={
                "email": "loadtest-attacker@example.com",
                "password": "DefinitelyWrongPassword!",
            },
            name="POST /v1/auth/login (invalid)",
            catch_response=True,
        ) as response:
            if response.status_code in {401, 429}:
                response.success()
            elif response.status_code == 200:
                response.failure("Invalid credentials were accepted.")
            else:
                response.failure(f"HTTP {response.status_code}")


class AuthenticatedUser(HttpUser):
    """
    Authenticated read traffic. Skipped unless LOAD_TEST_ACCESS_TOKEN is set
    to a short-lived access token.
    """

    weight = 1
    wait_time = between(0.5, 2.0)

    @task
    def profile(self) -> None:
        if not ACCESS_TOKEN:
            return

        self.client.get(
            "/v1/auth/me",
            headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
            name="GET /v1/auth/me",
        )

    @task
    def organizations(self) -> None:
        if not ACCESS_TOKEN:
            return

        self.client.get(
            "/v1/organizations",
            headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
            name="GET /v1/organizations",
        )
