from fastapi import (
    Depends,
    HTTPException,
    status,
)
from fastapi.security import APIKeyHeader

from app.core.config import get_settings
from app.services.admin_service import (
    AdminAuthenticationError,
    AdminService,
)

settings = get_settings()


admin_api_key_header = APIKeyHeader(
    name="X-TokenGuard-Admin-Key",
    scheme_name="TokenGuardAdminKey",
    description=("TokenGuard administrator API key beginning with tg_admin_."),
    auto_error=False,
)


async def require_admin(
    api_key: str | None = Depends(admin_api_key_header),
) -> bool:
    if not settings.require_admin_authentication:
        return True

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=("X-TokenGuard-Admin-Key header is required."),
        )

    service = AdminService()

    try:
        valid = service.validate_key(api_key)
    except AdminAuthenticationError as error:
        raise HTTPException(
            status_code=(status.HTTP_500_INTERNAL_SERVER_ERROR),
            detail=str(error),
        ) from error

    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid administrator API key.",
        )

    return True
