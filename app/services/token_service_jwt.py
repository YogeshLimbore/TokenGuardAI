"""
JWT access-token and opaque refresh-token management.

Design constraints:
- JWTs contain only identity and authorization references (sub, type,
  jti, iat, nbf, exp, iss, aud). They never carry passwords, provider
  keys, or sensitive profile data.
- Refresh tokens are opaque; only their SHA-256 hash is stored in the
  database.
- Never log access tokens, refresh tokens, or JWT payloads containing
  user data.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import (
    UTC,
    datetime,
    timedelta,
)
from uuid import uuid4

import jwt
from jwt import (
    ExpiredSignatureError,
    InvalidTokenError,
)

from app.core.config import get_settings


class AccessTokenInvalidError(RuntimeError):
    """Raised when an access token cannot be decoded or validated."""


@dataclass(frozen=True)
class TokenPair:
    """Bundles a freshly minted access/refresh token pair."""

    access_token: str
    refresh_token: str
    access_token_expires_at: datetime
    refresh_token_expires_at: datetime
    refresh_token_id: str


class JWTService:
    """Issues and validates JWT access tokens and opaque refresh tokens."""

    def __init__(self) -> None:
        self.settings = get_settings()

    # ------------------------------------------------------------------
    # Access tokens
    # ------------------------------------------------------------------

    def create_access_token(
        self,
        *,
        user_id: int,
        session_version: int = 1,
    ) -> tuple[str, datetime]:
        """
        Return a signed access token and its expiry datetime.

        The JWT ``sub`` claim carries the user ID as a string to follow
        the RFC 7519 recommendation that ``sub`` is a string.
        """
        now = datetime.now(UTC)

        expires_at = now + timedelta(minutes=self.settings.access_token_expire_minutes)

        payload = {
            "sub": str(user_id),
            "type": "access",
            "jti": str(uuid4()),
            "iat": now,
            "nbf": now,
            "exp": expires_at,
            "iss": self.settings.jwt_issuer,
            "aud": self.settings.jwt_audience,
            "ver": session_version,
        }

        token = jwt.encode(
            payload,
            self.settings.jwt_secret_key,
            algorithm=self.settings.jwt_algorithm,
        )

        return token, expires_at

    def decode_access_token(
        self,
        token: str,
    ) -> dict[str, object]:
        """
        Decode and validate a signed access token.

        Raises:
            AccessTokenInvalidError: if the token is expired, has an
                invalid signature, or carries the wrong type claim.
        """
        try:
            payload = jwt.decode(
                token,
                self.settings.jwt_secret_key,
                algorithms=[self.settings.jwt_algorithm],
                audience=self.settings.jwt_audience,
                issuer=self.settings.jwt_issuer,
            )
        except ExpiredSignatureError as error:
            raise AccessTokenInvalidError("Access token has expired.") from error
        except InvalidTokenError as error:
            raise AccessTokenInvalidError("Access token is invalid.") from error

        if payload.get("type") != "access":
            raise AccessTokenInvalidError("Unexpected token type.")

        return payload

    # ------------------------------------------------------------------
    # Refresh tokens
    # ------------------------------------------------------------------

    def create_refresh_token(
        self,
    ) -> tuple[str, str, datetime]:
        """
        Return (plaintext_token, token_id, expires_at).

        The plaintext token is returned only once and must be sent to the
        client immediately. Only the SHA-256 hash of the plaintext should
        be persisted.
        """
        token_id = str(uuid4())

        plaintext_token = f"{token_id}.{secrets.token_urlsafe(48)}"

        expires_at = datetime.now(UTC) + timedelta(
            days=self.settings.refresh_token_expire_days
        )

        return plaintext_token, token_id, expires_at

    @staticmethod
    def hash_refresh_token(token: str) -> str:
        """Return the SHA-256 hex digest of a plaintext refresh token."""
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def create_token_pair(
        self,
        *,
        user_id: int,
        session_version: int = 1,
    ) -> TokenPair:
        """Create and return a new access + refresh token pair."""
        access_token, access_expires = self.create_access_token(
            user_id=user_id, session_version=session_version
        )

        (
            refresh_token,
            refresh_token_id,
            refresh_expires,
        ) = self.create_refresh_token()

        return TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            access_token_expires_at=access_expires,
            refresh_token_expires_at=refresh_expires,
            refresh_token_id=refresh_token_id,
        )
