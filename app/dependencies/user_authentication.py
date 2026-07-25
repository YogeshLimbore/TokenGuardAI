"""
FastAPI dependency: resolves the authenticated user from the bearer token.

Used as a parameter dependency:

    user: User = Depends(get_current_user)

Swagger will expose the OAuth2 bearer flow through FastAPI's OpenAPI
security integration when ``OAuth2PasswordBearer`` is declared here.
"""

from fastapi import (
    Depends,
    HTTPException,
    status,
)
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.database.connection import get_database_session
from app.database.models import User
from app.services.token_service_jwt import (
    AccessTokenInvalidError,
    JWTService,
)

oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="/v1/auth/token",
    auto_error=False,
)


async def get_current_user(
    token: str | None = Depends(oauth2_scheme),
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

    return user
