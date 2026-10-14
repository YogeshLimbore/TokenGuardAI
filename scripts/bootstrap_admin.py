"""Create the initial platform administrator without persisting a password."""

import argparse
import getpass
import os
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from app.core.config import get_settings
from app.database.connection import SessionLocal
from app.services.identity_service import IdentityService


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create the first TokenGuard platform administrator."
    )
    parser.add_argument(
        "--email",
        default=os.getenv("BOOTSTRAP_ADMIN_EMAIL"),
        help="Administrator email (or set BOOTSTRAP_ADMIN_EMAIL).",
    )
    parser.add_argument(
        "--password-file",
        type=Path,
        default=(
            Path(value)
            if (value := os.getenv("BOOTSTRAP_ADMIN_PASSWORD_FILE"))
            else None
        ),
        help="Read the initial password from a mounted secret file.",
    )
    return parser.parse_args()


def _password(password_file: Path | None) -> str:
    if password_file is not None:
        password = password_file.read_text(encoding="utf-8").strip()
    elif sys.stdin.isatty():
        password = getpass.getpass("Initial administrator password: ")
        if password != getpass.getpass("Confirm password: "):
            raise RuntimeError("Passwords do not match.")
    else:
        raise RuntimeError(
            "Use --password-file with a mounted secret when no terminal is available."
        )
    if len(password) < 12:
        raise RuntimeError("The initial password must contain at least 12 characters.")
    return password


def main() -> None:
    arguments = _arguments()
    if not arguments.email:
        raise RuntimeError("--email or BOOTSTRAP_ADMIN_EMAIL is required.")

    # Validate the normal application configuration, including production
    # guards, while keeping the one-time password outside Settings.
    get_settings()
    password = _password(arguments.password_file)
    database = SessionLocal()
    try:
        user = IdentityService().create_user(
            database,
            email=arguments.email,
            display_name="Platform Administrator",
            password=password,
            is_platform_admin=True,
            is_email_verified=True,
        )
        print(f"Created platform administrator: {user.email}")
    finally:
        password = ""
        database.close()


if __name__ == "__main__":
    main()
