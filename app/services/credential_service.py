from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.database.models import (
    CredentialAuditLog,
    ProviderCredential,
)
from app.services.encryption_service import EncryptionService


class CredentialNotFoundError(LookupError):
    pass


class DuplicateCredentialError(ValueError):
    pass


class CredentialService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.encryption_service = EncryptionService()

    @staticmethod
    def mask_api_key(api_key: str) -> str:
        cleaned_key = api_key.strip()

        if len(cleaned_key) <= 8:
            return "•" * len(cleaned_key)

        prefix_length = min(8, len(cleaned_key) - 4)
        prefix = cleaned_key[:prefix_length]
        suffix = cleaned_key[-4:]

        return f"{prefix}{'•' * 12}{suffix}"

    @staticmethod
    def _write_audit_log(
        database: Session,
        *,
        project_id: int | None,
        credential_id: int | None,
        provider: str,
        action: str,
        succeeded: bool = True,
        details: str | None = None,
    ) -> None:
        audit_log = CredentialAuditLog(
            project_id=project_id,
            credential_id=credential_id,
            provider=provider,
            action=action,
            succeeded=succeeded,
            details=details,
        )

        database.add(audit_log)

    @staticmethod
    def get_by_id(
        database: Session,
        credential_id: int,
        *,
        project_id: int,
    ) -> ProviderCredential:
        statement = select(ProviderCredential).where(
            ProviderCredential.id == credential_id,
            ProviderCredential.project_id == project_id,
        )

        credential = database.scalar(statement)

        if credential is None:
            raise CredentialNotFoundError("Credential was not found in this project.")

        return credential

    @staticmethod
    def _clear_existing_default(
        database: Session,
        *,
        provider: str,
        project_id: int,
        except_credential_id: int | None = None,
    ) -> None:
        statement = select(ProviderCredential).where(
            ProviderCredential.provider == provider,
            ProviderCredential.project_id == project_id,
            ProviderCredential.is_default.is_(True),
        )

        if except_credential_id is not None:
            statement = statement.where(ProviderCredential.id != except_credential_id)

        existing_defaults = list(database.scalars(statement).all())

        for credential in existing_defaults:
            credential.is_default = False

    @staticmethod
    def get_default_credential(
        database: Session,
        *,
        provider: str,
        project_id: int,
    ) -> ProviderCredential | None:
        statement = (
            select(ProviderCredential)
            .where(
                ProviderCredential.provider == provider.lower(),
                ProviderCredential.project_id == project_id,
                ProviderCredential.is_active.is_(True),
                ProviderCredential.is_default.is_(True),
            )
            .order_by(ProviderCredential.updated_at.desc())
            .limit(1)
        )

        return database.scalar(statement)

    @staticmethod
    def get_active_credentials(
        database: Session,
        *,
        provider: str,
        project_id: int,
        exclude_ids: set[int] | None = None,
    ) -> list[ProviderCredential]:
        statement = (
            select(ProviderCredential)
            .where(
                ProviderCredential.provider == provider.lower(),
                ProviderCredential.project_id == project_id,
                ProviderCredential.is_active.is_(True),
            )
            .order_by(
                ProviderCredential.is_default.desc(),
                ProviderCredential.last_test_succeeded.desc(),
                ProviderCredential.updated_at.desc(),
            )
        )

        credentials = list(database.scalars(statement).all())

        if exclude_ids:
            credentials = [
                credential
                for credential in credentials
                if credential.id not in exclude_ids
            ]

        return credentials

    def create(
        self,
        database: Session,
        *,
        project_id: int,
        provider: str,
        name: str,
        api_key: str,
        is_default: bool = False,
    ) -> ProviderCredential:
        normalized_provider = provider.strip().lower()
        normalized_name = name.strip()

        duplicate_statement = select(ProviderCredential).where(
            ProviderCredential.project_id == project_id,
            ProviderCredential.provider == normalized_provider,
            ProviderCredential.name == normalized_name,
        )

        duplicate = database.scalar(duplicate_statement)

        if duplicate is not None:
            raise DuplicateCredentialError(
                "A credential with this provider and name already exists."
            )

        if is_default:
            self._clear_existing_default(
                database,
                provider=normalized_provider,
                project_id=project_id,
            )

        encrypted_api_key = self.encryption_service.encrypt(api_key)

        credential = ProviderCredential(
            project_id=project_id,
            provider=normalized_provider,
            name=normalized_name,
            encrypted_api_key=encrypted_api_key,
            masked_key=self.mask_api_key(api_key),
            is_active=True,
            is_default=is_default,
        )

        try:
            database.add(credential)
            database.flush()

            self._write_audit_log(
                database,
                project_id=project_id,
                credential_id=credential.id,
                provider=credential.provider,
                action="created",
                details="Credential created.",
            )

            database.commit()
            database.refresh(credential)
            return credential

        except Exception:
            database.rollback()
            raise

    @staticmethod
    def list_credentials(
        database: Session,
        *,
        project_id: int,
        include_inactive: bool = True,
    ) -> list[ProviderCredential]:
        statement = (
            select(ProviderCredential)
            .where(
                ProviderCredential.project_id == project_id,
            )
            .order_by(ProviderCredential.created_at.desc())
        )

        if not include_inactive:
            statement = statement.where(ProviderCredential.is_active.is_(True))

        return list(database.scalars(statement).all())

    def update(
        self,
        database: Session,
        *,
        credential_id: int,
        project_id: int,
        name: str | None,
        is_active: bool | None,
        is_default: bool | None = None,
    ) -> ProviderCredential:
        credential = self.get_by_id(
            database,
            credential_id,
            project_id=project_id,
        )

        changed_fields: list[str] = []

        if name is not None:
            cleaned_name = name.strip()

            duplicate_statement = select(ProviderCredential).where(
                ProviderCredential.project_id == project_id,
                ProviderCredential.provider == credential.provider,
                ProviderCredential.name == cleaned_name,
                ProviderCredential.id != credential.id,
            )

            if database.scalar(duplicate_statement) is not None:
                raise DuplicateCredentialError(
                    "Another credential already uses this provider and name."
                )

            credential.name = cleaned_name
            changed_fields.append("name")

        if is_default is True:
            self._clear_existing_default(
                database,
                provider=credential.provider,
                project_id=project_id,
                except_credential_id=credential.id,
            )

            credential.is_default = True
            credential.is_active = True
            changed_fields.append("is_default")

        elif is_default is False:
            credential.is_default = False
            changed_fields.append("is_default")

        if is_active is not None:
            credential.is_active = is_active

            if not is_active:
                credential.is_default = False

            changed_fields.append("is_active")

        credential.updated_at = datetime.now(UTC)

        try:
            self._write_audit_log(
                database,
                project_id=project_id,
                credential_id=credential.id,
                provider=credential.provider,
                action="updated",
                details=(
                    "Changed fields: " + ", ".join(changed_fields)
                    if changed_fields
                    else "No fields changed."
                ),
            )

            database.commit()
            database.refresh(credential)
            return credential

        except Exception:
            database.rollback()
            raise

    def rotate(
        self,
        database: Session,
        *,
        credential_id: int,
        project_id: int,
        new_api_key: str,
    ) -> ProviderCredential:
        credential = self.get_by_id(
            database,
            credential_id,
            project_id=project_id,
        )

        credential.encrypted_api_key = self.encryption_service.encrypt(new_api_key)
        credential.masked_key = self.mask_api_key(new_api_key)
        credential.last_test_succeeded = None
        credential.last_test_message = None
        credential.last_tested_at = None
        credential.updated_at = datetime.now(UTC)

        try:
            self._write_audit_log(
                database,
                project_id=project_id,
                credential_id=credential.id,
                provider=credential.provider,
                action="rotated",
                details="Credential secret was rotated.",
            )

            database.commit()
            database.refresh(credential)
            return credential

        except Exception:
            database.rollback()
            raise

    def delete(
        self,
        database: Session,
        *,
        credential_id: int,
        project_id: int,
    ) -> None:
        credential = self.get_by_id(
            database,
            credential_id,
            project_id=project_id,
        )

        provider = credential.provider

        try:
            # Preserve the audit record without preserving
            # the encrypted credential itself.
            self._write_audit_log(
                database,
                project_id=project_id,
                credential_id=None,
                provider=provider,
                action="deleted",
                details=(f"Credential record {credential_id} deleted."),
            )

            database.delete(credential)
            database.commit()

        except Exception:
            database.rollback()
            raise

    def decrypt_api_key(
        self,
        database: Session,
        *,
        credential_id: int,
        project_id: int,
        require_active: bool = True,
    ) -> tuple[ProviderCredential, str]:
        credential = self.get_by_id(
            database,
            credential_id,
            project_id=project_id,
        )

        if require_active and not credential.is_active:
            raise ValueError("The selected credential is inactive.")

        plaintext_api_key = self.encryption_service.decrypt(
            credential.encrypted_api_key
        )

        return credential, plaintext_api_key

    async def test_credential(
        self,
        database: Session,
        *,
        credential_id: int,
        project_id: int,
    ) -> tuple[ProviderCredential, bool, str]:
        credential, api_key = self.decrypt_api_key(
            database,
            credential_id=credential_id,
            project_id=project_id,
            require_active=False,
        )

        valid = False
        message = "Provider testing is not implemented."

        if credential.provider == "openrouter":
            valid, message = await self._test_openrouter(api_key)
        elif credential.provider == "groq":
            valid, message = await self._test_groq(api_key)
        else:
            from app.providers.registry import get_provider

            spec = get_provider(credential.provider)
            headers = {"Authorization": f"Bearer {api_key}"}
            endpoint = spec.base_url.rstrip("/") + "/models"
            if credential.provider == "anthropic":
                endpoint = spec.base_url.rstrip("/") + "/v1/models"
                headers = {
                    "x-api-key": api_key,
                    "anthropic-version": self.settings.anthropic_version,
                }
            try:
                async with httpx.AsyncClient(
                    timeout=self.settings.credential_test_timeout_seconds,
                    follow_redirects=False,
                ) as client:
                    response = await client.get(endpoint, headers=headers)
                valid = response.status_code == 200
                message = (
                    "Provider accepted the credential."
                    if valid
                    else f"Provider credential probe returned HTTP {response.status_code}."
                )
            except httpx.RequestError:
                message = "Provider credential probe is unavailable."

        tested_at = datetime.now(UTC)

        credential.last_test_succeeded = valid
        credential.last_test_message = message
        credential.last_tested_at = tested_at
        credential.updated_at = tested_at

        try:
            self._write_audit_log(
                database,
                project_id=project_id,
                credential_id=credential.id,
                provider=credential.provider,
                action="tested",
                succeeded=valid,
                details=message,
            )

            database.commit()
            database.refresh(credential)

        except Exception:
            database.rollback()
            raise

        return credential, valid, message

    async def _test_openrouter(
        self,
        api_key: str,
    ) -> tuple[bool, str]:
        endpoint = f"{self.settings.openrouter_base_url.rstrip('/')}/key"

        headers = {
            "Authorization": f"Bearer {api_key}",
        }

        try:
            async with httpx.AsyncClient(
                timeout=(self.settings.credential_test_timeout_seconds)
            ) as client:
                response = await client.get(
                    endpoint,
                    headers=headers,
                )

        except httpx.TimeoutException:
            return False, ("OpenRouter credential test timed out.")

        except httpx.ConnectError:
            return False, ("Could not connect to OpenRouter.")

        if response.status_code == 200:
            return True, ("OpenRouter accepted the credential.")

        if response.status_code == 401:
            return False, ("OpenRouter rejected the credential.")

        if response.status_code == 429:
            return False, ("OpenRouter rate limit was exceeded.")

        return False, (f"OpenRouter returned HTTP {response.status_code}.")

    async def _test_groq(
        self,
        api_key: str,
    ) -> tuple[bool, str]:
        endpoint = f"{self.settings.groq_base_url.rstrip('/')}/models"

        headers = {
            "Authorization": f"Bearer {api_key}",
        }

        try:
            async with httpx.AsyncClient(
                timeout=(self.settings.credential_test_timeout_seconds)
            ) as client:
                response = await client.get(
                    endpoint,
                    headers=headers,
                )

        except httpx.TimeoutException:
            return False, ("Groq credential test timed out.")

        except httpx.ConnectError:
            return False, ("Could not connect to Groq.")

        if response.status_code == 200:
            return True, ("Groq accepted the credential.")

        if response.status_code == 401:
            return False, ("Groq rejected the credential.")

        if response.status_code == 429:
            return False, ("Groq rate limit was exceeded.")

        return False, (f"Groq returned HTTP {response.status_code}.")

    @staticmethod
    def list_audit_logs(
        database: Session,
        *,
        project_id: int,
        credential_id: int | None = None,
        limit: int = 100,
    ) -> list[CredentialAuditLog]:
        statement = (
            select(CredentialAuditLog)
            .where(
                CredentialAuditLog.project_id == project_id,
            )
            .order_by(CredentialAuditLog.created_at.desc())
            .limit(limit)
        )

        if credential_id is not None:
            statement = statement.where(
                CredentialAuditLog.credential_id == credential_id
            )

        return list(database.scalars(statement).all())
