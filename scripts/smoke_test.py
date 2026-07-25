"""
Post-deployment smoke test.

Checks that a running deployment answers on the endpoints a client actually
depends on. Read-only: it never sends a chat completion, so it costs nothing
and cannot pollute usage data.

    $env:TOKEN_GUARD_API_BASE_URL = "https://api.example.com"
    $env:SMOKE_TEST_PROJECT_KEY = "tg_live_..."   # optional
    uv run python scripts/smoke_test.py

Without SMOKE_TEST_PROJECT_KEY only unauthenticated checks run.
"""

from __future__ import annotations

import os
import sys

import httpx

TIMEOUT_SECONDS = 20.0

ACCEPTABLE_HEALTH_STATUSES = {"healthy", "degraded"}


class SmokeTestError(RuntimeError):
    pass


def _base_url() -> str:
    return os.environ.get(
        "TOKEN_GUARD_API_BASE_URL",
        "http://127.0.0.1:8000",
    ).rstrip("/")


def check_health(client: httpx.Client) -> None:
    response = client.get("/health")
    response.raise_for_status()

    payload = response.json()
    status = payload.get("status")

    if status not in ACCEPTABLE_HEALTH_STATUSES:
        raise SmokeTestError(
            f"Health status is {status!r}; "
            f"expected one of {sorted(ACCEPTABLE_HEALTH_STATUSES)}."
        )

    print(f"  health: {status} (version {payload.get('version', 'unknown')})")

    unhealthy: list[str] = []

    for name in ("database", "redis"):
        detail = payload.get(name)

        if not isinstance(detail, dict):
            continue

        dependency_status = detail.get("status")
        print(f"    {name}: {dependency_status}")

        if dependency_status != "healthy":
            unhealthy.append(name)

    # PostgreSQL is authoritative for credentials, projects, and budgets, so
    # the deployment is not usable without it even if /health says degraded.
    if "database" in unhealthy:
        raise SmokeTestError(
            "PostgreSQL is unreachable; the deployment cannot serve traffic."
        )

    if unhealthy:
        print("  ! Degraded but serving. Unhealthy: " + ", ".join(unhealthy))


def check_metrics(client: httpx.Client) -> None:
    response = client.get("/metrics")

    if response.status_code == httpx.codes.NOT_FOUND:
        print("  metrics: disabled (ENABLE_PROMETHEUS_METRICS is false)")
        return

    response.raise_for_status()

    if "tokenguard_http_requests_total" not in response.text:
        raise SmokeTestError(
            "The metrics endpoint is exposed but publishes no "
            "tokenguard_http_requests_total series."
        )

    print("  metrics: exposed")


def check_authentication_is_enforced(
    client: httpx.Client,
) -> None:
    """An unauthenticated project endpoint must not return data."""
    response = client.get("/v1/analytics/summary")

    if response.status_code not in {
        httpx.codes.UNAUTHORIZED,
        httpx.codes.FORBIDDEN,
    }:
        raise SmokeTestError(
            "Unauthenticated request to /v1/analytics/summary returned "
            f"HTTP {response.status_code}; expected 401 or 403. "
            "Project authentication may be disabled."
        )

    print("  authentication: enforced")


def check_project_access(
    client: httpx.Client,
    project_key: str,
) -> None:
    response = client.get(
        "/v1/analytics/summary",
        headers={"X-TokenGuard-Key": project_key},
    )
    response.raise_for_status()

    print("  project key: accepted")


def main() -> int:
    base_url = _base_url()
    project_key = os.environ.get("SMOKE_TEST_PROJECT_KEY")

    print(f"Smoke testing {base_url}")

    with httpx.Client(
        base_url=base_url,
        timeout=TIMEOUT_SECONDS,
        follow_redirects=True,
    ) as client:
        check_health(client)
        check_metrics(client)
        check_authentication_is_enforced(client)

        if project_key:
            check_project_access(client, project_key)
        else:
            print("  project key: skipped (SMOKE_TEST_PROJECT_KEY not set)")

    print("\nSmoke tests passed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (
        SmokeTestError,
        httpx.HTTPError,
    ) as error:
        print(
            f"\nSmoke tests failed: {error}",
            file=sys.stderr,
        )
        sys.exit(1)
