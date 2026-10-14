from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

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
        params: dict[str, str | int | float | bool | None] | None = None,
        json_body: dict[str, object] | None = None,
    ) -> object:
        endpoint = f"{self.base_url}{path}"

        try:
            response = httpx.request(
                method=method,
                url=endpoint,
                headers=headers,
                params=params,
                json=json_body,
                timeout=self.timeout_seconds,
                follow_redirects=False,
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

        if response.status_code == 204:
            return None
        return response.json()

    @staticmethod
    def project_headers(
        *,
        access_token: str | None = None,
        project_id: int | None = None,
        project_api_key: str | None = None,
    ) -> dict[str, str]:
        if access_token and project_id is not None:
            return {
                "Authorization": f"Bearer {access_token}",
                "X-TokenGuard-Project-ID": str(project_id),
            }
        if project_api_key:
            return {"X-TokenGuard-Key": project_api_key}
        raise DashboardAPIError("Project authentication is required.")

    def login(self, *, email: str, password: str) -> dict[str, object]:
        result = self._request(
            method="POST",
            path="/v1/auth/login",
            headers={},
            json_body={"email": email, "password": password},
        )
        if not isinstance(result, dict) or not isinstance(
            result.get("access_token"), str
        ):
            raise DashboardAPIError("Invalid login response.")
        return result

    def refresh(self, *, refresh_token: str) -> dict[str, object]:
        result = self._request(
            method="POST",
            path="/v1/auth/refresh",
            headers={},
            json_body={"refresh_token": refresh_token},
        )
        if not isinstance(result, dict) or not isinstance(
            result.get("access_token"), str
        ):
            raise DashboardAPIError("Invalid token-refresh response.")
        return result

    def logout(self, *, refresh_token: str) -> None:
        self._request(
            method="POST",
            path="/v1/auth/logout",
            headers={},
            json_body={"refresh_token": refresh_token},
        )

    def change_password(
        self, *, access_token: str, current_password: str, new_password: str
    ) -> None:
        self._request(
            method="POST",
            path="/v1/auth/password",
            headers={"Authorization": f"Bearer {access_token}"},
            json_body={
                "current_password": current_password,
                "new_password": new_password,
            },
        )

    def list_sessions(self, *, access_token: str) -> list[dict[str, object]]:
        result = self._request(
            method="GET",
            path="/v1/auth/sessions",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if not isinstance(result, list):
            raise DashboardAPIError("Invalid session-list response.")
        return result

    def revoke_session(self, *, access_token: str, token_id: str) -> None:
        self._request(
            method="DELETE",
            path=f"/v1/auth/sessions/{quote(token_id, safe='')}",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    def revoke_all_sessions(self, *, access_token: str) -> None:
        self._request(
            method="DELETE",
            path="/v1/auth/sessions",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    def list_organizations(self, *, access_token: str) -> list[dict[str, object]]:
        result = self._request(
            method="GET",
            path="/v1/organizations",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if not isinstance(result, list):
            raise DashboardAPIError("Invalid organization-list response.")
        return result

    def list_organization_projects(
        self, *, access_token: str, organization_id: int
    ) -> list[dict[str, object]]:
        result = self._request(
            method="GET",
            path=f"/v1/organizations/{organization_id}/projects",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if not isinstance(result, list):
            raise DashboardAPIError("Invalid project-list response.")
        return result

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
        project_api_key: str | None = None,
        access_token: str | None = None,
        project_id: int | None = None,
        days: int = 30,
    ) -> dict[str, object]:
        result = self._request(
            method="GET",
            path="/v1/analytics/summary",
            headers=self.project_headers(
                access_token=access_token,
                project_id=project_id,
                project_api_key=project_api_key,
            ),
            params={
                "days": days,
                "recent_request_limit": 100,
            },
        )

        if not isinstance(result, dict):
            raise DashboardAPIError("Invalid analytics response.")

        return result

    def list_providers(
        self, *, access_token: str | None, project_id: int, project_api_key: str | None
    ) -> list[dict[str, object]]:
        result = self._request(
            method="GET",
            path="/v1/providers",
            headers=self.project_headers(
                access_token=access_token,
                project_id=project_id,
                project_api_key=project_api_key,
            ),
        )
        if not isinstance(result, list):
            raise DashboardAPIError("Invalid provider-list response.")
        return result

    def list_credentials(
        self, *, access_token: str | None, project_id: int, project_api_key: str | None
    ) -> list[dict[str, object]]:
        result = self._request(
            method="GET",
            path="/v1/credentials",
            headers=self.project_headers(
                access_token=access_token,
                project_id=project_id,
                project_api_key=project_api_key,
            ),
        )
        if not isinstance(result, list):
            raise DashboardAPIError("Invalid credential-list response.")
        return result

    def create_credential(
        self,
        *,
        access_token: str | None,
        project_id: int,
        project_api_key: str | None,
        provider: str,
        name: str,
        api_key: str,
        is_default: bool,
    ) -> dict[str, object]:
        result = self._request(
            method="POST",
            path="/v1/credentials",
            headers=self.project_headers(
                access_token=access_token,
                project_id=project_id,
                project_api_key=project_api_key,
            ),
            json_body={
                "provider": provider,
                "name": name,
                "api_key": api_key,
                "is_default": is_default,
            },
        )
        if not isinstance(result, dict):
            raise DashboardAPIError("Invalid credential response.")
        return result

    def test_credential(
        self,
        *,
        access_token: str | None,
        project_id: int,
        project_api_key: str | None,
        credential_id: int,
    ) -> dict[str, object]:
        result = self._request(
            method="POST",
            path=f"/v1/credentials/{credential_id}/test",
            headers=self.project_headers(
                access_token=access_token,
                project_id=project_id,
                project_api_key=project_api_key,
            ),
        )
        if not isinstance(result, dict):
            raise DashboardAPIError("Invalid credential-test response.")
        return result

    def delete_credential(
        self,
        *,
        access_token: str | None,
        project_id: int,
        project_api_key: str | None,
        credential_id: int,
    ) -> None:
        self._request(
            method="DELETE",
            path=f"/v1/credentials/{credential_id}",
            headers=self.project_headers(
                access_token=access_token,
                project_id=project_id,
                project_api_key=project_api_key,
            ),
        )

    def list_models(
        self,
        *,
        access_token: str | None,
        project_id: int,
        project_api_key: str | None,
        provider: str,
        credential_id: int | None,
    ) -> dict[str, object]:
        result = self._request(
            method="GET",
            path=f"/v1/providers/{provider}/models",
            headers=self.project_headers(
                access_token=access_token,
                project_id=project_id,
                project_api_key=project_api_key,
            ),
            params={"credential_id": credential_id} if credential_id else None,
        )
        if not isinstance(result, dict):
            raise DashboardAPIError("Invalid model-list response.")
        return result

    def list_model_prices(
        self,
        *,
        access_token: str | None,
        project_id: int,
        project_api_key: str | None,
    ) -> list[dict[str, object]]:
        result = self._request(
            method="GET",
            path="/v1/providers/pricing",
            headers=self.project_headers(
                access_token=access_token,
                project_id=project_id,
                project_api_key=project_api_key,
            ),
        )
        if not isinstance(result, list):
            raise DashboardAPIError("Invalid model-pricing response.")
        return result

    def save_model_price(
        self,
        *,
        access_token: str | None,
        project_id: int,
        project_api_key: str | None,
        provider: str,
        model: str,
        input_price: float,
        output_price: float,
    ) -> dict[str, object]:
        result = self._request(
            method="PUT",
            path=f"/v1/providers/pricing/{quote(provider, safe='')}",
            headers=self.project_headers(
                access_token=access_token,
                project_id=project_id,
                project_api_key=project_api_key,
            ),
            json_body={
                "model": model,
                "input_usd_per_1m_tokens": input_price,
                "output_usd_per_1m_tokens": output_price,
            },
        )
        if not isinstance(result, dict):
            raise DashboardAPIError("Invalid model-pricing response.")
        return result

    def delete_model_price(
        self,
        *,
        access_token: str | None,
        project_id: int,
        project_api_key: str | None,
        provider: str,
        model: str,
    ) -> None:
        self._request(
            method="DELETE",
            path=(
                f"/v1/providers/pricing/{quote(provider, safe='')}/"
                f"{quote(model, safe='')}"
            ),
            headers=self.project_headers(
                access_token=access_token,
                project_id=project_id,
                project_api_key=project_api_key,
            ),
        )
