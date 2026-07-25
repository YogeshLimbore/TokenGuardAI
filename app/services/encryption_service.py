from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings


class EncryptionConfigurationError(RuntimeError):
    """Raised when the vault master key is missing or invalid."""


class CredentialDecryptionError(RuntimeError):
    """Raised when an encrypted credential cannot be decrypted."""


class EncryptionService:
    def __init__(self) -> None:
        settings = get_settings()
        master_key = settings.token_guard_master_key

        if not master_key:
            raise EncryptionConfigurationError(
                "TOKEN_GUARD_MASTER_KEY is not configured."
            )

        try:
            self._fernet = Fernet(master_key.strip().encode("utf-8"))
        except (TypeError, ValueError) as error:
            raise EncryptionConfigurationError(
                "TOKEN_GUARD_MASTER_KEY is invalid. "
                "Generate it using Fernet.generate_key()."
            ) from error

    def encrypt(self, plaintext: str) -> str:
        cleaned_value = plaintext.strip()

        if not cleaned_value:
            raise ValueError("The credential value cannot be empty.")

        encrypted_value = self._fernet.encrypt(cleaned_value.encode("utf-8"))

        return encrypted_value.decode("utf-8")

    def decrypt(self, encrypted_value: str) -> str:
        try:
            plaintext = self._fernet.decrypt(encrypted_value.encode("utf-8"))
        except InvalidToken as error:
            raise CredentialDecryptionError(
                "The credential could not be decrypted. "
                "The master key may have changed or the data may be invalid."
            ) from error

        return plaintext.decode("utf-8")
