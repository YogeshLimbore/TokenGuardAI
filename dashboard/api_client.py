from __future__ import annotations

from dataclasses import dataclass

import httpx


class DashboardAPIError(RuntimeError):
    pass


@dataclass
class ProjectOption:
    id: int
    name: str
    is_active: bool


class TokenGuardAPIClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def _request(
        self,
        *,
        method: str,
        path: str,
        headers: dict[str, str],
        params: dict[str, object] | None = None,
    ) -> object:
        endpoint = f"{self.base_url}{path}"

        try:
            response = httpx.request(
                method=method,
                url=endpoint,
                headers=headers,
                params=params,
                timeout=self.timeout_seconds,
            )
        except httpx.ConnectError as error:
            raise DashboardAPIError("Could not connect to TokenGuard API.") from error
        except httpx.TimeoutException as error:
            raise DashboardAPIError("TokenGuard API request timed out.") from error

        if response.status_code >= 400:
            try:
                error_data = response.json()
            except ValueError:
                error_data = response.text

            raise DashboardAPIError(
                f"TokenGuard returned HTTP {response.status_code}: {error_data}"
            )

        return response.json()

    def list_projects(
        self,
        *,
        admin_api_key: str,
    ) -> list[dict[str, object]]:
        result = self._request(
            method="GET",
            path="/v1/projects",
            headers={
                "X-TokenGuard-Admin-Key": (admin_api_key),
            },
        )

        if not isinstance(result, list):
            raise DashboardAPIError("Invalid project-list response.")

        return result

    def get_project_analytics(
        self,
        *,
        project_api_key: str,
        days: int = 30,
    ) -> dict[str, object]:
        result = self._request(
            method="GET",
            path="/v1/analytics/summary",
            headers={
                "X-TokenGuard-Key": (project_api_key),
            },
            params={
                "days": days,
                "recent_request_limit": 100,
            },
        )

        if not isinstance(result, dict):
            raise DashboardAPIError("Invalid analytics response.")

        return result
