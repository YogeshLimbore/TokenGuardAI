import hashlib
import secrets
from datetime import UTC, datetime

from app.core.config import get_settings


class ProjectKeyService:
    def __init__(self) -> None:
        self.settings = get_settings()

    def generate_key(self) -> str:
        random_part = secrets.token_urlsafe(self.settings.project_api_key_bytes)

        return f"{self.settings.project_api_key_prefix}{random_part}"

    @staticmethod
    def hash_key(api_key: str) -> str:
        return hashlib.sha256(api_key.encode("utf-8")).hexdigest()

    @staticmethod
    def extract_prefix(api_key: str) -> str:
        return api_key[:16]

    @staticmethod
    def mask_key(api_key: str) -> str:
        if len(api_key) <= 12:
            return "•" * len(api_key)

        return f"{api_key[:10]}{'•' * 12}{api_key[-4:]}"

    @staticmethod
    def is_expired(
        expires_at: datetime | None,
    ) -> bool:
        if expires_at is None:
            return False

        now = datetime.now(UTC)

        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)

        return expires_at <= now
