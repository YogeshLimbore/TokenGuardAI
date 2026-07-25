import hashlib
import hmac

from app.core.config import get_settings


class AdminAuthenticationError(RuntimeError):
    pass


class AdminService:
    def __init__(self) -> None:
        self.settings = get_settings()

    @staticmethod
    def hash_key(api_key: str) -> str:
        return hashlib.sha256(api_key.encode("utf-8")).hexdigest()

    def validate_key(self, api_key: str) -> bool:
        configured_hash = self.settings.admin_api_key_hash

        if not configured_hash:
            raise AdminAuthenticationError("ADMIN_API_KEY_HASH is not configured.")

        supplied_hash = self.hash_key(api_key)

        return hmac.compare_digest(
            supplied_hash,
            configured_hash,
        )
