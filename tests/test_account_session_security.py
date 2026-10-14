from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database.models import User
from tests.conftest import TEST_PASSWORD


def _login(client: TestClient, user: User, password: str) -> dict[str, object]:
    response = client.post(
        "/v1/auth/login", json={"email": user.email, "password": password}
    )
    assert response.status_code == 200
    return response.json()


def test_password_change_revokes_all_tokens_and_sessions(
    client: TestClient, database: Session, platform_admin_user: User
) -> None:
    tokens = _login(client, platform_admin_user, TEST_PASSWORD)
    old_access = str(tokens["access_token"])
    headers = {"Authorization": f"Bearer {old_access}"}

    sessions = client.get("/v1/auth/sessions", headers=headers)
    assert sessions.status_code == 200
    assert len(sessions.json()) == 1

    wrong = client.post(
        "/v1/auth/password",
        headers=headers,
        json={
            "current_password": "Wrong-Password-123!",
            "new_password": "Replacement-Password-123!",
        },
    )
    assert wrong.status_code == 400

    changed = client.post(
        "/v1/auth/password",
        headers=headers,
        json={
            "current_password": TEST_PASSWORD,
            "new_password": "Replacement-Password-123!",
        },
    )
    assert changed.status_code == 204
    assert client.get("/v1/auth/me", headers=headers).status_code == 401
    assert (
        client.post(
            "/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        ).status_code
        == 401
    )

    new_tokens = _login(client, platform_admin_user, "Replacement-Password-123!")
    new_headers = {"Authorization": f"Bearer {new_tokens['access_token']}"}
    assert client.get("/v1/auth/me", headers=new_headers).status_code == 200


def test_individual_and_bulk_session_revocation(
    client: TestClient, database: Session, platform_admin_user: User
) -> None:
    first = _login(client, platform_admin_user, TEST_PASSWORD)
    second = _login(client, platform_admin_user, TEST_PASSWORD)
    headers = {"Authorization": f"Bearer {second['access_token']}"}
    sessions = client.get("/v1/auth/sessions", headers=headers).json()
    assert len(sessions) == 2

    missing = client.delete("/v1/auth/sessions/missing", headers=headers)
    assert missing.status_code == 404

    revoked = client.delete(
        f"/v1/auth/sessions/{sessions[0]['token_id']}", headers=headers
    )
    assert revoked.status_code == 204

    bulk = client.delete("/v1/auth/sessions", headers=headers)
    assert bulk.status_code == 204
    assert (
        client.post(
            "/v1/auth/refresh", json={"refresh_token": first["refresh_token"]}
        ).status_code
        == 401
    )


def test_platform_admin_can_provision_verified_users(
    client: TestClient, database: Session, platform_admin_user: User
) -> None:
    admin_tokens = _login(client, platform_admin_user, TEST_PASSWORD)
    admin_headers = {"Authorization": f"Bearer {admin_tokens['access_token']}"}
    request = {
        "email": "provisioned@example.com",
        "display_name": "Provisioned User",
        "password": "Provisioned-Password-123!",
        "is_email_verified": True,
        "is_platform_admin": False,
    }
    created = client.post("/v1/auth/users", headers=admin_headers, json=request)
    assert created.status_code == 201
    assert created.json()["is_email_verified"] is True
    assert created.json()["is_platform_admin"] is False
    assert (
        client.post("/v1/auth/users", headers=admin_headers, json=request).status_code
        == 409
    )

    users = client.get("/v1/auth/users", headers=admin_headers)
    assert users.status_code == 200
    assert {record["email"] for record in users.json()} >= {
        platform_admin_user.email,
        "provisioned@example.com",
    }

    provisioned = database.get(User, created.json()["id"])
    assert provisioned is not None
    user_tokens = _login(client, provisioned, "Provisioned-Password-123!")
    user_headers = {"Authorization": f"Bearer {user_tokens['access_token']}"}
    assert client.get("/v1/auth/users", headers=user_headers).status_code == 403
