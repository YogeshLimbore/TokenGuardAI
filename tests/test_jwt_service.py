from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.config import get_settings
from app.services.token_service_jwt import AccessTokenInvalidError, JWTService


def test_jwt_access_token_lifecycle():
    service = JWTService()
    user_id = 42

    token, expires_at = service.create_access_token(user_id=user_id)
    assert isinstance(token, str)
    assert expires_at > datetime.now(UTC)

    payload = service.decode_access_token(token)
    assert payload["sub"] == str(user_id)
    assert payload["type"] == "access"


def test_jwt_invalid_token():
    service = JWTService()
    with pytest.raises(AccessTokenInvalidError):
        service.decode_access_token("invalid.jwt.token")


def test_refresh_token_generation():
    service = JWTService()
    plaintext, token_id, expires_at = service.create_refresh_token()

    assert plaintext.startswith(token_id)
    assert expires_at > datetime.now(UTC)

    hash1 = service.hash_refresh_token(plaintext)
    hash2 = service.hash_refresh_token(plaintext)
    assert hash1 == hash2


def test_expired_access_token_is_rejected():
    settings = get_settings()
    service = JWTService()

    now = datetime.now(UTC)
    payload = {
        "sub": "1",
        "type": "access",
        "jti": "expired-token",
        "iat": now - timedelta(minutes=30),
        "nbf": now - timedelta(minutes=30),
        "exp": now - timedelta(minutes=1),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
    }
    token = jwt.encode(
        payload,
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )

    with pytest.raises(AccessTokenInvalidError):
        service.decode_access_token(token)


def test_wrong_issuer_is_rejected():
    settings = get_settings()
    service = JWTService()

    now = datetime.now(UTC)
    payload = {
        "sub": "1",
        "type": "access",
        "jti": "wrong-issuer",
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(minutes=5),
        "iss": "some-other-issuer",
        "aud": settings.jwt_audience,
    }
    token = jwt.encode(
        payload,
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )

    with pytest.raises(AccessTokenInvalidError):
        service.decode_access_token(token)


def test_wrong_audience_is_rejected():
    settings = get_settings()
    service = JWTService()

    now = datetime.now(UTC)
    payload = {
        "sub": "1",
        "type": "access",
        "jti": "wrong-audience",
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(minutes=5),
        "iss": settings.jwt_issuer,
        "aud": "some-other-audience",
    }
    token = jwt.encode(
        payload,
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )

    with pytest.raises(AccessTokenInvalidError):
        service.decode_access_token(token)


def test_refresh_token_cannot_be_used_as_access_token():
    """A refresh token is an opaque string, not a JWT, so decoding it as
    an access token must fail rather than silently succeeding."""
    service = JWTService()
    plaintext, _, _ = service.create_refresh_token()

    with pytest.raises(AccessTokenInvalidError):
        service.decode_access_token(plaintext)


def test_wrong_type_claim_is_rejected():
    """A JWT that is well-formed and signed but carries a non-"access"
    type claim (e.g. a hypothetical refresh-as-JWT) must be rejected."""
    settings = get_settings()
    service = JWTService()

    now = datetime.now(UTC)
    payload = {
        "sub": "1",
        "type": "refresh",
        "jti": "wrong-type",
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(minutes=5),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
    }
    token = jwt.encode(
        payload,
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )

    with pytest.raises(AccessTokenInvalidError):
        service.decode_access_token(token)
