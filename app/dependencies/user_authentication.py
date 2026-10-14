"""Resolve an authenticated user from an HTTP bearer access token."""

from datetime import UTC

from fastapi import (
    Depends,
    HTTPException,
    status,
)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.database.connection import get_database_session
from app.database.models import User
from app.services.token_service_jwt import (
    AccessTokenInvalidError,
    JWTService,
)

oauth2_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    token: HTTPAuthorizationCredentials | str | None = Depends(oauth2_scheme),
    database: Session = Depends(get_database_session),
) -> User:
    """
    Decode the bearer token, load the corresponding user, and return it.

    Raises HTTP 401 if:
    - no token is provided
    - the token is invalid or expired
    - the subject is not a valid integer user ID
    - the user no longer exists or is inactive
    """
    if isinstance(token, HTTPAuthorizationCredentials):
        token = token.credentials
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer access token is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    service = JWTService()

    try:
        payload = service.decode_access_token(token)
    except AccessTokenInvalidError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(error),
            headers={"WWW-Authenticate": "Bearer"},
        ) from error

    subject = payload.get("sub")

    try:
        user_id = int(str(subject))
    except (TypeError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Access token subject is invalid.",
        ) from error

    user = database.get(User, user_id)

    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User is inactive or missing.",
        )

    token_version = payload.get("ver")
    if (
        isinstance(token_version, bool)
        or not isinstance(token_version, int)
        or token_version != user.session_version
    ):
        raise HTTPException(status_code=401, detail="Session has been revoked.")

    from app.core.config import get_settings

    issued_at = payload.get("iat")
    if isinstance(issued_at, bool) or not isinstance(issued_at, int | float):
        raise HTTPException(
            status_code=401, detail="Access token issue time is invalid."
        )
    # JWT NumericDate values are encoded with whole-second precision. Compare
    # at that same precision so a token issued immediately after account
    # creation/password rotation is not treated as older by microseconds.
    if user.password_changed_at and int(issued_at) < int(
        user.password_changed_at.replace(tzinfo=UTC).timestamp()
    ):
        raise HTTPException(status_code=401, detail="Session has been revoked.")
    if get_settings().require_email_verification and not user.is_email_verified:
        raise HTTPException(status_code=403, detail="Email verification is required.")
    return user
