"""
Authentication API — login, token refresh, logout, and user profile.

Endpoints:
    POST  /v1/auth/register  — create account (if public registration is on)
    POST  /v1/auth/login     — exchange credentials for a token pair
    POST  /v1/auth/refresh   — rotate the refresh token
    POST  /v1/auth/logout    — revoke the refresh token
    GET   /v1/auth/me        — return the current user's profile
"""

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    status,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cache.redis_client import get_redis_client
from app.core.config import get_settings
from app.database.connection import get_database_session
from app.database.models import User
from app.dependencies.user_authentication import get_current_user
from app.schemas.identity import (
    LoginRequest,
    LogoutRequest,
    PasswordChangeRequest,
    RefreshTokenRequest,
    SessionResponse,
    TokenResponse,
    UserCreateRequest,
    UserProvisionRequest,
    UserResponse,
)
from app.services.auth_rate_limit_service import (
    AuthRateLimitExceededError,
    AuthRateLimitService,
)
from app.services.identity_service import (
    AccountLockedError,
    IdentityService,
    InvalidCredentialsError,
    RefreshTokenInvalidError,
)


def _client_ip(http_request: Request) -> str:
    # Uvicorn applies forwarded headers only from configured trusted proxies.
    return http_request.client.host if http_request.client else "unknown"


def _enforce_auth_rate_limit(
    *,
    scope: str,
    identifier: str,
    limit: int,
) -> None:
    rate_limiter = AuthRateLimitService(get_redis_client())

    try:
        rate_limiter.enforce(
            scope=scope,
            identifier=identifier,
            limit=limit,
        )
    except AuthRateLimitExceededError as error:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(error),
            headers={"Retry-After": str(error.retry_after_seconds)},
        ) from error


router = APIRouter(
    prefix="/v1/auth",
    tags=["Identity"],
)


def user_to_response(user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_active=user.is_active,
        is_email_verified=user.is_email_verified,
        is_platform_admin=user.is_platform_admin,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
    )


def _require_platform_admin(user: User) -> None:
    if not user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform administrator required.")


@router.get("/users", response_model=list[UserResponse])
async def list_users(
    user: User = Depends(get_current_user),
    database: Session = Depends(get_database_session),
) -> list[UserResponse]:
    _require_platform_admin(user)
    users = database.scalars(select(User).order_by(User.email)).all()
    return [user_to_response(record) for record in users]


@router.post(
    "/users",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
)
async def provision_user(
    request: UserProvisionRequest,
    user: User = Depends(get_current_user),
    database: Session = Depends(get_database_session),
) -> UserResponse:
    _require_platform_admin(user)
    try:
        created = IdentityService().create_user(
            database,
            email=request.email,
            display_name=request.display_name,
            password=request.password,
            is_platform_admin=request.is_platform_admin,
            is_email_verified=request.is_email_verified,
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return user_to_response(created)


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new user account",
    description=(
        "Creates a new user. Only available when ``ALLOW_PUBLIC_REGISTRATION=true``."
    ),
)
async def register(
    request: UserCreateRequest,
    database: Session = Depends(get_database_session),
) -> UserResponse:
    settings = get_settings()

    if not settings.allow_public_registration:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Public registration is disabled.",
        )

    service = IdentityService()

    try:
        user = service.create_user(
            database,
            email=request.email,
            display_name=request.display_name,
            password=request.password,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error

    return user_to_response(user)


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Authenticate and receive a token pair",
)
async def login(
    request: LoginRequest,
    http_request: Request,
    database: Session = Depends(get_database_session),
) -> TokenResponse:
    settings = get_settings()
    ip_address = _client_ip(http_request)

    _enforce_auth_rate_limit(
        scope="login",
        identifier=ip_address,
        limit=settings.auth_login_rate_limit_per_minute,
    )

    service = IdentityService()

    try:
        _, token_pair = service.authenticate(
            database,
            email=request.email,
            password=request.password,
            user_agent=http_request.headers.get("User-Agent"),
            ip_address=ip_address,
        )
    except (InvalidCredentialsError, AccountLockedError) as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(error),
        ) from error

    return TokenResponse(
        access_token=token_pair.access_token,
        refresh_token=token_pair.refresh_token,
        access_token_expires_at=token_pair.access_token_expires_at,
        refresh_token_expires_at=token_pair.refresh_token_expires_at,
    )


@router.post(
    "/token",
    response_model=TokenResponse,
    include_in_schema=False,  # alias used by Swagger OAuth2 flow
)
async def token_alias(
    request: LoginRequest,
    http_request: Request,
    database: Session = Depends(get_database_session),
) -> TokenResponse:
    """Alias of /login consumed by the Swagger UI OAuth2 form."""
    return await login(request, http_request, database)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Rotate the refresh token",
    description=(
        "Accepts a valid refresh token and returns a new token pair. "
        "The old refresh token is immediately revoked."
    ),
)
async def refresh(
    request: RefreshTokenRequest,
    http_request: Request,
    database: Session = Depends(get_database_session),
) -> TokenResponse:
    settings = get_settings()

    _enforce_auth_rate_limit(
        scope="refresh",
        identifier=_client_ip(http_request),
        limit=settings.auth_refresh_rate_limit_per_minute,
    )

    service = IdentityService()

    try:
        token_pair = service.rotate_refresh_token(
            database,
            plaintext_refresh_token=request.refresh_token,
        )
    except RefreshTokenInvalidError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(error),
        ) from error

    return TokenResponse(
        access_token=token_pair.access_token,
        refresh_token=token_pair.refresh_token,
        access_token_expires_at=token_pair.access_token_expires_at,
        refresh_token_expires_at=token_pair.refresh_token_expires_at,
    )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke the refresh token and end the session",
)
async def logout(
    request: LogoutRequest,
    database: Session = Depends(get_database_session),
) -> None:
    service = IdentityService()

    service.revoke_refresh_token(
        database,
        plaintext_refresh_token=request.refresh_token,
    )


@router.get(
    "/me",
    response_model=UserResponse,
    summary="Return the authenticated user's profile",
)
async def get_me(
    user: User = Depends(get_current_user),
) -> UserResponse:
    return user_to_response(user)


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    request: PasswordChangeRequest,
    user: User = Depends(get_current_user),
    database: Session = Depends(get_database_session),
) -> None:
    try:
        IdentityService().change_password(
            database,
            user_id=user.id,
            current_password=request.current_password,
            new_password=request.new_password,
        )
    except InvalidCredentialsError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/sessions", response_model=list[SessionResponse])
async def list_sessions(
    user: User = Depends(get_current_user),
    database: Session = Depends(get_database_session),
) -> list[SessionResponse]:
    return [
        SessionResponse(
            token_id=record.token_id,
            expires_at=record.expires_at,
            revoked_at=record.revoked_at,
            created_at=record.created_at,
        )
        for record in IdentityService.list_sessions(database, user_id=user.id)
    ]


@router.delete("/sessions", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_all_sessions(
    user: User = Depends(get_current_user),
    database: Session = Depends(get_database_session),
) -> None:
    IdentityService().revoke_sessions(database, user_id=user.id)


@router.delete("/sessions/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(
    token_id: str,
    user: User = Depends(get_current_user),
    database: Session = Depends(get_database_session),
) -> None:
    if not IdentityService().revoke_sessions(
        database, user_id=user.id, token_id=token_id
    ):
        raise HTTPException(status_code=404, detail="Session was not found.")
