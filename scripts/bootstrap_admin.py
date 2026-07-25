"""
Bootstrap the initial platform administrator account.

Usage:
    BOOTSTRAP_ADMIN_EMAIL=admin@example.com BOOTSTRAP_ADMIN_PASSWORD=secret uv run python scripts/bootstrap_admin.py
"""

import sys
from pathlib import Path

# Add project root to python path when running script directly
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from app.core.config import get_settings
from app.database.connection import SessionLocal
from app.services.identity_service import IdentityService


def main() -> None:
    settings = get_settings()

    if not settings.bootstrap_admin_email or not settings.bootstrap_admin_password:
        raise RuntimeError(
            "BOOTSTRAP_ADMIN_EMAIL and BOOTSTRAP_ADMIN_PASSWORD are required."
        )

    database = SessionLocal()

    try:
        service = IdentityService()

        user = service.create_user(
            database,
            email=settings.bootstrap_admin_email,
            display_name="Platform Administrator",
            password=settings.bootstrap_admin_password,
            is_platform_admin=True,
        )

        print(
            f"Created platform administrator: {user.email}\n"
            "REMINDER: Please remove BOOTSTRAP_ADMIN_PASSWORD from your environment / .env file."
        )
    except ValueError as error:
        print(f"Bootstrap skipped/failed: {error}")
    finally:
        database.close()


if __name__ == "__main__":
    main()
