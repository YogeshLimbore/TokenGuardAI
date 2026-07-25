from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.database.connection import Base
from app.database.models import ProviderAttemptLog
from app.providers.base import ProviderResponse
from app.providers.openrouter import OpenRouterProviderError
from app.schemas.chat import ChatMessage
from app.services.credential_service import CredentialService
from app.services.fallback_service import (
    AllProviderAttemptsFailedError,
    FallbackService,
    ProviderAttempt,
)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()

    yield session

    session.close()


def test_build_attempts_ollama(db_session: Session):
    service = FallbackService()

    attempts = service.build_attempts(
        db_session,
        project_id=1,
        selected_provider="ollama",
        selected_model="llama3.2",
        explicit_credential_id=None,
    )

    assert len(attempts) == 1
    assert attempts[0].provider == "ollama"
    assert attempts[0].credential_source == "not_required"


def test_build_attempts_with_default_and_backup(db_session: Session):
    cred_service = CredentialService()

    # Create default credential
    cred1 = cred_service.create(
        db_session,
        project_id=1,
        provider="openrouter",
        name="Default Key",
        api_key="sk-or-v1-1234567890",
        is_default=True,
    )

    # Create backup credential
    cred2 = cred_service.create(
        db_session,
        project_id=1,
        provider="openrouter",
        name="Backup Key",
        api_key="sk-or-v1-0987654321",
        is_default=False,
    )

    service = FallbackService()

    attempts = service.build_attempts(
        db_session,
        project_id=1,
        selected_provider="openrouter",
        selected_model="openai/gpt-4.1-mini",
        explicit_credential_id=None,
    )

    assert len(attempts) == 3
    assert attempts[0].credential_id == cred1.id
    assert attempts[0].credential_source == "default"
    assert attempts[1].credential_id == cred2.id
    assert attempts[1].credential_source == "backup"
    assert attempts[2].provider == "ollama"
    assert attempts[2].credential_source == "fallback_local"


def test_build_attempts_with_explicit_credential(db_session: Session):
    cred_service = CredentialService()

    cred1 = cred_service.create(
        db_session,
        project_id=1,
        provider="openrouter",
        name="Key 1",
        api_key="sk-or-v1-1234567890",
        is_default=True,
    )

    cred2 = cred_service.create(
        db_session,
        project_id=1,
        provider="openrouter",
        name="Key 2",
        api_key="sk-or-v1-0987654321",
        is_default=False,
    )

    service = FallbackService()

    attempts = service.build_attempts(
        db_session,
        project_id=1,
        selected_provider="openrouter",
        selected_model="openai/gpt-4.1-mini",
        explicit_credential_id=cred2.id,
    )

    assert len(attempts) == 3
    assert attempts[0].credential_id == cred2.id
    assert attempts[0].credential_source == "explicit"
    assert attempts[1].credential_id == cred1.id
    assert attempts[1].credential_source == "backup"
    assert attempts[2].provider == "ollama"
    assert attempts[2].credential_source == "fallback_local"


@pytest.mark.anyio
async def test_execute_fallback_to_ollama_success(db_session: Session):
    service = FallbackService()

    attempts = [
        ProviderAttempt(
            provider="openrouter",
            model="openai/gpt-4.1-mini",
            credential_id=1,
            credential_source="default",
            api_key="fake-key",
        ),
        ProviderAttempt(
            provider="ollama",
            model="llama3.2",
            credential_id=None,
            credential_source="fallback_local",
            api_key=None,
        ),
    ]

    messages = [ChatMessage(role="user", content="Hello")]

    mock_openrouter_provider = AsyncMock()
    mock_openrouter_provider.generate.side_effect = OpenRouterProviderError(
        "Insufficient credits"
    )

    mock_ollama_provider = AsyncMock()
    mock_ollama_provider.generate.return_value = ProviderResponse(
        content="Response from Ollama",
        model="llama3.2",
        input_tokens=10,
        output_tokens=15,
    )

    def factory_side_effect(provider_name, api_key=None):
        if provider_name == "openrouter":
            return mock_openrouter_provider
        return mock_ollama_provider

    with patch(
        "app.services.fallback_service.ProviderFactory.create",
        side_effect=factory_side_effect,
    ):
        result = await service.execute(
            db_session,
            project_id=1,
            request_id="req-test-123",
            attempts=attempts,
            messages=messages,
            temperature=0.3,
            max_tokens=100,
        )

    assert result.fallback_used is True
    assert result.attempts_made == 2
    assert result.provider == "ollama"
    assert result.response.content == "Response from Ollama"

    # Verify attempt logs in database
    logs = list(
        db_session.scalars(
            select(ProviderAttemptLog).order_by(ProviderAttemptLog.attempt_number)
        ).all()
    )
    assert len(logs) == 2
    assert logs[0].provider == "openrouter"
    assert logs[0].succeeded is False
    assert logs[0].error_message == "Insufficient credits"
    assert logs[1].provider == "ollama"
    assert logs[1].succeeded is True


@pytest.mark.anyio
async def test_execute_all_failed(db_session: Session):
    service = FallbackService()

    attempts = [
        ProviderAttempt(
            provider="ollama",
            model="llama3.2",
            credential_id=None,
            credential_source="not_required",
            api_key=None,
        ),
    ]

    messages = [ChatMessage(role="user", content="Hello")]

    mock_ollama_provider = AsyncMock()
    mock_ollama_provider.generate.side_effect = OpenRouterProviderError(
        "Ollama service down"
    )

    with patch(
        "app.services.fallback_service.ProviderFactory.create",
        return_value=mock_ollama_provider,
    ):
        with pytest.raises(AllProviderAttemptsFailedError) as exc_info:
            await service.execute(
                db_session,
                project_id=1,
                request_id="req-fail-456",
                attempts=attempts,
                messages=messages,
                temperature=0.3,
                max_tokens=100,
            )

    assert "All provider attempts failed." in str(exc_info.value)
    assert len(exc_info.value.errors) == 1
