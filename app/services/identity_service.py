"""
Identity service — user creation, authentication, and token management.

Security invariants enforced here:
- Login failures always return the same generic message regardless of
  whether the email exists or the password is wrong (prevents enumeration).
- Accounts are locked for LOCK_MINUTES after MAX_FAILED_ATTEMPTS bad logins.
- Refresh tokens are rotated on every use; the old record is revoked and
  tracks which token replaced it (``replaced_by_token_id``).
- Only the SHA-256 hash of a refresh token is stored; the plaintext is
  never persisted.
- Passwords, hashes, and raw tokens must never appear in logs or errors.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database.models import (
    IdentityAuditEvent,
    RefreshToken,
    User,
)
from app.observability.context import get_correlation_id
from app.services.password_service import PasswordService
from app.services.token_service_jwt import (
    JWTService,
    TokenPair,
)

logger = logging.getLogger(__name__)


class InvalidCredentialsError(RuntimeError):
    """Raised for incorrect email/password combinations."""


class AccountLockedError(RuntimeError):
    """Raised when the account is temporarily locked due to failed logins."""


class RefreshTokenInvalidError(RuntimeError):
    """Raised for expired, revoked, or unrecognised refresh tokens."""


class IdentityService:
    """Manages user lifecycle, authentication, and session tokens."""

    MAX_FAILED_ATTEMPTS = 5
    LOCK_MINUTES = 15

    def __init__(self) -> None:
        self.password_service = PasswordService()
        self.jwt_service = JWTService()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def normalize_email(email: str) -> str:
        return email.strip().lower()

    @staticmethod
    def _hash_optional(value: str | None) -> str | None:
        if not value:
            return None
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _record_audit_event(
        self,
        database: Session,
        *,
        event_type: str,
        succeeded: bool,
        user_id: int | None = None,
        organization_id: int | None = None,
    ) -> None:
        """Persist an identity audit event without blocking the caller."""
        try:
            event = IdentityAuditEvent(
                event_type=event_type,
                succeeded=succeeded,
                user_id=user_id,
                organization_id=organization_id,
                correlation_id=get_correlation_id(),
            )
            database.add(event)
            # Intentionally NOT committing here; the caller commits after
            # completing its own work so both are in the same transaction.
        except Exception:
            # Audit failure must never block authentication, but it must be
            # visible: a silently broken audit trail is a security problem.
            logger.exception(
                "Could not record identity audit event '%s'.",
                event_type,
            )

    # ------------------------------------------------------------------
    # User management
    # ------------------------------------------------------------------

    def create_user(
        self,
        database: Session,
        *,
        email: str,
        display_name: str,
        password: str,
        is_platform_admin: bool = False,
    ) -> User:
        """
        Create and persist a new user.

        Raises:
            ValueError: if a user with this email already exists.
        """
        user = User(
            email=self.normalize_email(email),
            display_name=display_name.strip(),
            password_hash=self.password_service.hash_password(password),
            is_active=True,
            is_email_verified=False,
            is_platform_admin=is_platform_admin,
        )

        try:
            database.add(user)
            database.commit()
            database.refresh(user)
            return user
        except IntegrityError as error:
            database.rollback()
            raise ValueError("A user with this email already exists.") from error

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def authenticate(
        self,
        database: Session,
        *,
        email: str,
        password: str,
        user_agent: str | None,
        ip_address: str | None,
    ) -> tuple[User, TokenPair]:
        """
        Validate credentials and return the user plus a new token pair.

        Raises:
            InvalidCredentialsError: for wrong email or password.
            AccountLockedError: when the account is temporarily locked.
        """
        statement = select(User).where(User.email == self.normalize_email(email))
        user = database.scalar(statement)

        if user is None or not user.is_active:
            # Keep timing consistent with a real password check.
            self.password_service.verify_password(
                password,
                "$argon2id$v=19$m=65536,t=3,p=4$placeholder$placeholder",
            )
            raise InvalidCredentialsError("Invalid email or password.")

        now = datetime.now(UTC)

        if (
            user.locked_until is not None
            and user.locked_until.replace(tzinfo=UTC) > now
        ):
            raise AccountLockedError("Account is temporarily locked.")

        verified = self.password_service.verify_password(
            password,
            user.password_hash,
        )

        if not verified:
            user.failed_login_attempts += 1

            if user.failed_login_attempts >= self.MAX_FAILED_ATTEMPTS:
                user.locked_until = now + timedelta(minutes=self.LOCK_MINUTES)

            self._record_audit_event(
                database,
                event_type="login_failed",
                succeeded=False,
                user_id=user.id,
            )
            database.commit()

            raise InvalidCredentialsError("Invalid email or password.")

        # Successful login — reset lockout counters.
        user.failed_login_attempts = 0
        user.locked_until = None
        user.last_login_at = now

        token_pair = self.jwt_service.create_token_pair(user_id=user.id)

        refresh_record = RefreshToken(
            token_id=token_pair.refresh_token_id,
            user_id=user.id,
            token_hash=self.jwt_service.hash_refresh_token(token_pair.refresh_token),
            expires_at=token_pair.refresh_token_expires_at,
            user_agent_hash=self._hash_optional(user_agent),
            ip_address_hash=self._hash_optional(ip_address),
        )

        database.add(refresh_record)
        self._record_audit_event(
            database,
            event_type="login_succeeded",
            succeeded=True,
            user_id=user.id,
        )
        database.commit()

        return user, token_pair

    # ------------------------------------------------------------------
    # Refresh-token rotation
    # ------------------------------------------------------------------

    def rotate_refresh_token(
        self,
        database: Session,
        *,
        plaintext_refresh_token: str,
    ) -> TokenPair:
        """
        Revoke the presented token and issue a new pair.

        Raises:
            RefreshTokenInvalidError: if the token is unknown, revoked,
                or expired.
        """
        token_hash = self.jwt_service.hash_refresh_token(plaintext_refresh_token)

        statement = select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        record = database.scalar(statement)

        now = datetime.now(UTC)

        if (
            record is None
            or record.revoked_at is not None
            or record.expires_at.replace(tzinfo=UTC) <= now
        ):
            raise RefreshTokenInvalidError("Refresh token is invalid.")

        user = database.get(User, record.user_id)

        if user is None or not user.is_active:
            raise RefreshTokenInvalidError("Refresh token is invalid.")

        new_pair = self.jwt_service.create_token_pair(user_id=user.id)

        # Revoke old record.
        record.revoked_at = now
        record.replaced_by_token_id = new_pair.refresh_token_id

        # Issue new record.
        database.add(
            RefreshToken(
                token_id=new_pair.refresh_token_id,
                user_id=user.id,
                token_hash=self.jwt_service.hash_refresh_token(new_pair.refresh_token),
                expires_at=new_pair.refresh_token_expires_at,
            )
        )

        self._record_audit_event(
            database,
            event_type="refresh_token_rotated",
            succeeded=True,
            user_id=user.id,
        )
        database.commit()

        return new_pair

    # ------------------------------------------------------------------
    # Logout / revocation
    # ------------------------------------------------------------------

    def revoke_refresh_token(
        self,
        database: Session,
        *,
        plaintext_refresh_token: str,
    ) -> None:
        """
        Mark a refresh token as revoked.

        This is a best-effort operation; an unknown token is silently
        ignored (idempotent logout).
        """
        token_hash = self.jwt_service.hash_refresh_token(plaintext_refresh_token)

        statement = select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        record = database.scalar(statement)

        if record is None:
            return

        record.revoked_at = datetime.now(UTC)
        database.commit()
