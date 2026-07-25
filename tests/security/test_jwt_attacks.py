"""
JWT and refresh-token attack surface.

These exercise the token validation path through the HTTP API rather than the
service in isolation, so a missing check anywhere in the dependency chain
(decode, claim validation, user lookup, active flag) is caught.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.database.models import RefreshToken, User
from app.services.identity_service import IdentityService
from app.services.token_service_jwt import JWTService
from tests.conftest import TEST_PASSWORD, access_token_for, create_user

pytestmark = [pytest.mark.security, pytest.mark.integration]


@pytest.fixture
def member_user(database: Session) -> User:
    return create_user(database, email="jwt-subject@example.com")


def _claims(*, user_id: int, **overrides) -> dict:
    settings = get_settings()
    now = datetime.now(UTC)

    claims = {
        "sub": str(user_id),
        "type": "access",
        "jti": "attack-token",
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(minutes=10),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
    }
    claims.update(overrides)

    return claims


def _encode(claims: dict, *, secret: str | None = None) -> str:
    settings = get_settings()

    return jwt.encode(
        claims,
        secret or settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )


def _profile(client: TestClient, token: str):
    return client.get(
        "/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )


def test_valid_token_is_accepted(
    client: TestClient,
    member_user: User,
) -> None:
    response = _profile(client, access_token_for(member_user))

    assert response.status_code == 200
    assert response.json()["email"] == member_user.email


def test_tampered_payload_is_rejected(
    client: TestClient,
    member_user: User,
    database: Session,
) -> None:
    other = create_user(database, email="jwt-victim@example.com")

    token = access_token_for(member_user)
    header, payload, signature = token.split(".")

    decoded = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    decoded["sub"] = str(other.id)

    forged_payload = (
        base64.urlsafe_b64encode(json.dumps(decoded).encode("utf-8"))
        .decode("utf-8")
        .rstrip("=")
    )

    response = _profile(client, f"{header}.{forged_payload}.{signature}")

    assert response.status_code == 401


def test_token_signed_with_the_wrong_secret_is_rejected(
    client: TestClient,
    member_user: User,
) -> None:
    token = _encode(
        _claims(user_id=member_user.id),
        secret="a-different-secret-that-is-at-least-48-characters-long",
    )

    assert _profile(client, token).status_code == 401


def test_unsigned_token_is_rejected(
    client: TestClient,
    member_user: User,
) -> None:
    """A ``none``-algorithm token must never be trusted."""
    token = jwt.encode(
        _claims(user_id=member_user.id),
        key="",
        algorithm="none",
    )

    assert _profile(client, token).status_code == 401


def test_wrong_audience_is_rejected(
    client: TestClient,
    member_user: User,
) -> None:
    token = _encode(_claims(user_id=member_user.id, aud="some-other-service"))

    assert _profile(client, token).status_code == 401


def test_wrong_issuer_is_rejected(
    client: TestClient,
    member_user: User,
) -> None:
    token = _encode(_claims(user_id=member_user.id, iss="https://attacker.example"))

    assert _profile(client, token).status_code == 401


def test_expired_token_is_rejected(
    client: TestClient,
    member_user: User,
) -> None:
    now = datetime.now(UTC)

    token = _encode(
        _claims(
            user_id=member_user.id,
            iat=now - timedelta(hours=2),
            nbf=now - timedelta(hours=2),
            exp=now - timedelta(hours=1),
        )
    )

    assert _profile(client, token).status_code == 401


def test_not_yet_valid_token_is_rejected(
    client: TestClient,
    member_user: User,
) -> None:
    now = datetime.now(UTC)

    token = _encode(
        _claims(
            user_id=member_user.id,
            nbf=now + timedelta(minutes=30),
            exp=now + timedelta(hours=1),
        )
    )

    assert _profile(client, token).status_code == 401


def test_wrong_token_type_is_rejected(
    client: TestClient,
    member_user: User,
) -> None:
    token = _encode(_claims(user_id=member_user.id, type="refresh"))

    assert _profile(client, token).status_code == 401


def test_opaque_refresh_token_cannot_be_used_as_access_token(
    client: TestClient,
    database: Session,
    member_user: User,
) -> None:
    _, pair = IdentityService().authenticate(
        database,
        email=member_user.email,
        password=TEST_PASSWORD,
        user_agent="pytest",
        ip_address="127.0.0.1",
    )

    assert _profile(client, pair.refresh_token).status_code == 401


def test_access_token_cannot_be_used_as_refresh_token(
    client: TestClient,
    member_user: User,
) -> None:
    response = client.post(
        "/v1/auth/refresh",
        json={"refresh_token": access_token_for(member_user)},
    )

    assert response.status_code == 401


def test_token_for_deactivated_user_is_rejected(
    client: TestClient,
    database: Session,
    member_user: User,
) -> None:
    token = access_token_for(member_user)
    assert _profile(client, token).status_code == 200

    member_user.is_active = False
    database.commit()

    assert _profile(client, token).status_code == 401


def test_token_for_deleted_user_is_rejected(
    client: TestClient,
    database: Session,
    member_user: User,
) -> None:
    token = access_token_for(member_user)

    database.delete(member_user)
    database.commit()

    assert _profile(client, token).status_code == 401


def test_malformed_bearer_values_are_rejected(
    client: TestClient,
) -> None:
    for value in ("", "Bearer", "Bearer ", "Bearer not-a-jwt", "abc.def"):
        response = client.get(
            "/v1/auth/me",
            headers={"Authorization": value},
        )
        assert response.status_code == 401


def test_rotated_refresh_token_cannot_be_reused(
    client: TestClient,
    member_user: User,
) -> None:
    login = client.post(
        "/v1/auth/login",
        json={"email": member_user.email, "password": TEST_PASSWORD},
    )
    original_refresh = login.json()["refresh_token"]

    first_rotation = client.post(
        "/v1/auth/refresh",
        json={"refresh_token": original_refresh},
    )
    assert first_rotation.status_code == 200

    replayed = client.post(
        "/v1/auth/refresh",
        json={"refresh_token": original_refresh},
    )

    assert replayed.status_code == 401


def test_logout_revokes_the_refresh_token(
    client: TestClient,
    member_user: User,
) -> None:
    login = client.post(
        "/v1/auth/login",
        json={"email": member_user.email, "password": TEST_PASSWORD},
    )
    refresh_token = login.json()["refresh_token"]

    client.post("/v1/auth/logout", json={"refresh_token": refresh_token})

    response = client.post(
        "/v1/auth/refresh",
        json={"refresh_token": refresh_token},
    )

    assert response.status_code == 401


def test_refresh_tokens_are_never_stored_in_plaintext(
    client: TestClient,
    database: Session,
    member_user: User,
) -> None:
    login = client.post(
        "/v1/auth/login",
        json={"email": member_user.email, "password": TEST_PASSWORD},
    )
    refresh_token = login.json()["refresh_token"]

    stored = list(
        database.scalars(
            select(RefreshToken).where(RefreshToken.user_id == member_user.id)
        )
    )

    assert stored
    expected_hash = JWTService.hash_refresh_token(refresh_token)

    for record in stored:
        assert record.token_hash != refresh_token
        assert refresh_token not in record.token_hash

    assert any(record.token_hash == expected_hash for record in stored)


def test_password_hash_is_never_returned_by_the_api(
    client: TestClient,
    member_user: User,
) -> None:
    login = client.post(
        "/v1/auth/login",
        json={"email": member_user.email, "password": TEST_PASSWORD},
    )
    profile = _profile(client, login.json()["access_token"])

    assert "password" not in profile.text.lower()
    assert member_user.password_hash not in login.text
    assert TEST_PASSWORD not in login.text
