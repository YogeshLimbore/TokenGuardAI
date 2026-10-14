"""
Provider credential lifecycle.

Credentials hold customer provider API keys, so every path here is
security-relevant: keys must be stored encrypted, masked for display, scoped to
a single project, and never resurrected from a deleted record's audit trail.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.database.models import CredentialAuditLog, ProviderCredential
from app.services.credential_service import (
    CredentialNotFoundError,
    CredentialService,
    DuplicateCredentialError,
)
from tests.conftest import create_project_with_key
from tests.fakes.http import (
    install_mock_transport,
    json_responder,
    raising_responder,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

OPENROUTER_KEY = "sk-or-v1-testonly000000000000000000000000"
GROQ_KEY = "gsk_testonly0000000000000000000000000000"


@pytest.fixture
def service() -> CredentialService:
    return CredentialService()


@pytest.fixture
def project_id(database: Session) -> int:
    project, _ = create_project_with_key(
        database,
        name="Credential Test Project",
    )
    return project.id


@pytest.fixture
def other_project_id(database: Session) -> int:
    project, _ = create_project_with_key(
        database,
        name="Other Credential Project",
    )
    return project.id


def _audit_actions(
    database: Session,
    project_id: int,
) -> list[str]:
    return [
        row.action
        for row in database.scalars(
            select(CredentialAuditLog)
            .where(CredentialAuditLog.project_id == project_id)
            .order_by(CredentialAuditLog.id)
        ).all()
    ]


# ---------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("api_key", "expected"),
    [
        ("", ""),
        ("short", "•••••"),
        ("12345678", "••••••••"),
        ("123456789", "12345" + "•" * 12 + "6789"),
        (
            "sk-or-v1-abcdefghijklmnop",
            "sk-or-v1" + "•" * 12 + "mnop",
        ),
    ],
)
def test_mask_api_key_never_reveals_the_middle(
    api_key: str,
    expected: str,
) -> None:
    assert CredentialService.mask_api_key(api_key) == expected


def test_mask_api_key_trims_surrounding_whitespace() -> None:
    assert CredentialService.mask_api_key(
        f"  {OPENROUTER_KEY}  "
    ) == CredentialService.mask_api_key(OPENROUTER_KEY)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def test_create_stores_the_key_encrypted_and_masked(
    database: Session,
    service: CredentialService,
    project_id: int,
) -> None:
    credential = service.create(
        database,
        project_id=project_id,
        provider="OpenRouter",
        name="  Primary  ",
        api_key=OPENROUTER_KEY,
    )

    assert credential.provider == "openrouter"
    assert credential.name == "Primary"
    assert credential.is_active is True
    assert credential.is_default is False

    assert credential.encrypted_api_key != OPENROUTER_KEY
    assert OPENROUTER_KEY not in credential.encrypted_api_key
    assert OPENROUTER_KEY not in credential.masked_key
    assert credential.masked_key.endswith(OPENROUTER_KEY[-4:])

    assert _audit_actions(database, project_id) == ["created"]


def test_create_rejects_a_duplicate_provider_and_name(
    database: Session,
    service: CredentialService,
    project_id: int,
) -> None:
    service.create(
        database,
        project_id=project_id,
        provider="openrouter",
        name="Primary",
        api_key=OPENROUTER_KEY,
    )

    with pytest.raises(DuplicateCredentialError):
        service.create(
            database,
            project_id=project_id,
            provider="openrouter",
            name="Primary",
            api_key=OPENROUTER_KEY,
        )


def test_the_same_name_is_allowed_for_a_different_provider(
    database: Session,
    service: CredentialService,
    project_id: int,
) -> None:
    service.create(
        database,
        project_id=project_id,
        provider="openrouter",
        name="Primary",
        api_key=OPENROUTER_KEY,
    )

    groq_credential = service.create(
        database,
        project_id=project_id,
        provider="groq",
        name="Primary",
        api_key=GROQ_KEY,
    )

    assert groq_credential.provider == "groq"


def test_the_same_name_is_allowed_in_a_different_project(
    database: Session,
    service: CredentialService,
    project_id: int,
    other_project_id: int,
) -> None:
    service.create(
        database,
        project_id=project_id,
        provider="openrouter",
        name="Primary",
        api_key=OPENROUTER_KEY,
    )

    foreign = service.create(
        database,
        project_id=other_project_id,
        provider="openrouter",
        name="Primary",
        api_key=OPENROUTER_KEY,
    )

    assert foreign.project_id == other_project_id


def test_creating_a_new_default_demotes_the_previous_one(
    database: Session,
    service: CredentialService,
    project_id: int,
) -> None:
    first = service.create(
        database,
        project_id=project_id,
        provider="openrouter",
        name="First",
        api_key=OPENROUTER_KEY,
        is_default=True,
    )

    second = service.create(
        database,
        project_id=project_id,
        provider="openrouter",
        name="Second",
        api_key=OPENROUTER_KEY,
        is_default=True,
    )

    database.refresh(first)

    assert first.is_default is False
    assert second.is_default is True


def test_create_rolls_back_when_the_flush_fails(
    database: Session,
    service: CredentialService,
    project_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_flush(*args: object, **kwargs: object) -> None:
        raise RuntimeError("flush failed")

    monkeypatch.setattr(database, "flush", failing_flush)

    with pytest.raises(RuntimeError, match="flush failed"):
        service.create(
            database,
            project_id=project_id,
            provider="openrouter",
            name="Doomed",
            api_key=OPENROUTER_KEY,
        )

    monkeypatch.undo()

    assert service.list_credentials(database, project_id=project_id) == []


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def test_get_by_id_is_scoped_to_the_owning_project(
    database: Session,
    service: CredentialService,
    project_id: int,
    other_project_id: int,
) -> None:
    credential = service.create(
        database,
        project_id=project_id,
        provider="openrouter",
        name="Primary",
        api_key=OPENROUTER_KEY,
    )

    assert (
        service.get_by_id(
            database,
            credential.id,
            project_id=project_id,
        ).id
        == credential.id
    )

    with pytest.raises(CredentialNotFoundError):
        service.get_by_id(
            database,
            credential.id,
            project_id=other_project_id,
        )


def test_get_by_id_rejects_an_unknown_credential(
    database: Session,
    service: CredentialService,
    project_id: int,
) -> None:
    with pytest.raises(CredentialNotFoundError):
        service.get_by_id(
            database,
            999_999,
            project_id=project_id,
        )


def test_list_credentials_can_hide_inactive_records(
    database: Session,
    service: CredentialService,
    project_id: int,
) -> None:
    active = service.create(
        database,
        project_id=project_id,
        provider="openrouter",
        name="Active",
        api_key=OPENROUTER_KEY,
    )
    inactive = service.create(
        database,
        project_id=project_id,
        provider="openrouter",
        name="Inactive",
        api_key=OPENROUTER_KEY,
    )

    service.update(
        database,
        credential_id=inactive.id,
        project_id=project_id,
        name=None,
        is_active=False,
    )

    all_ids = {
        credential.id
        for credential in service.list_credentials(
            database,
            project_id=project_id,
        )
    }
    active_ids = {
        credential.id
        for credential in service.list_credentials(
            database,
            project_id=project_id,
            include_inactive=False,
        )
    }

    assert all_ids == {active.id, inactive.id}
    assert active_ids == {active.id}


def test_get_default_credential_ignores_other_projects(
    database: Session,
    service: CredentialService,
    project_id: int,
    other_project_id: int,
) -> None:
    service.create(
        database,
        project_id=other_project_id,
        provider="openrouter",
        name="Foreign Default",
        api_key=OPENROUTER_KEY,
        is_default=True,
    )

    assert (
        service.get_default_credential(
            database,
            provider="openrouter",
            project_id=project_id,
        )
        is None
    )

    mine = service.create(
        database,
        project_id=project_id,
        provider="openrouter",
        name="Mine",
        api_key=OPENROUTER_KEY,
        is_default=True,
    )

    found = service.get_default_credential(
        database,
        provider="OpenRouter",
        project_id=project_id,
    )

    assert found is not None
    assert found.id == mine.id


def test_get_default_credential_skips_deactivated_records(
    database: Session,
    service: CredentialService,
    project_id: int,
) -> None:
    credential = service.create(
        database,
        project_id=project_id,
        provider="openrouter",
        name="Primary",
        api_key=OPENROUTER_KEY,
        is_default=True,
    )

    service.update(
        database,
        credential_id=credential.id,
        project_id=project_id,
        name=None,
        is_active=False,
    )

    assert (
        service.get_default_credential(
            database,
            provider="openrouter",
            project_id=project_id,
        )
        is None
    )


def test_active_credentials_are_ordered_with_the_default_first(
    database: Session,
    service: CredentialService,
    project_id: int,
) -> None:
    service.create(
        database,
        project_id=project_id,
        provider="openrouter",
        name="Secondary",
        api_key=OPENROUTER_KEY,
    )
    default = service.create(
        database,
        project_id=project_id,
        provider="openrouter",
        name="Default",
        api_key=OPENROUTER_KEY,
        is_default=True,
    )

    credentials = service.get_active_credentials(
        database,
        provider="openrouter",
        project_id=project_id,
    )

    assert credentials[0].id == default.id
    assert len(credentials) == 2


def test_active_credentials_honour_the_exclusion_set(
    database: Session,
    service: CredentialService,
    project_id: int,
) -> None:
    first = service.create(
        database,
        project_id=project_id,
        provider="openrouter",
        name="First",
        api_key=OPENROUTER_KEY,
    )
    second = service.create(
        database,
        project_id=project_id,
        provider="openrouter",
        name="Second",
        api_key=OPENROUTER_KEY,
    )

    remaining = service.get_active_credentials(
        database,
        provider="openrouter",
        project_id=project_id,
        exclude_ids={first.id},
    )

    assert [credential.id for credential in remaining] == [second.id]


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


def test_update_renames_a_credential(
    database: Session,
    service: CredentialService,
    project_id: int,
) -> None:
    credential = service.create(
        database,
        project_id=project_id,
        provider="openrouter",
        name="Old Name",
        api_key=OPENROUTER_KEY,
    )

    updated = service.update(
        database,
        credential_id=credential.id,
        project_id=project_id,
        name="  New Name  ",
        is_active=None,
    )

    assert updated.name == "New Name"
    assert _audit_actions(database, project_id) == ["created", "updated"]


def test_update_rejects_a_name_used_by_a_sibling(
    database: Session,
    service: CredentialService,
    project_id: int,
) -> None:
    service.create(
        database,
        project_id=project_id,
        provider="openrouter",
        name="Taken",
        api_key=OPENROUTER_KEY,
    )
    other = service.create(
        database,
        project_id=project_id,
        provider="openrouter",
        name="Free",
        api_key=OPENROUTER_KEY,
    )

    with pytest.raises(DuplicateCredentialError):
        service.update(
            database,
            credential_id=other.id,
            project_id=project_id,
            name="Taken",
            is_active=None,
        )


def test_promoting_a_credential_reactivates_it_and_demotes_the_rest(
    database: Session,
    service: CredentialService,
    project_id: int,
) -> None:
    current_default = service.create(
        database,
        project_id=project_id,
        provider="openrouter",
        name="Current",
        api_key=OPENROUTER_KEY,
        is_default=True,
    )
    challenger = service.create(
        database,
        project_id=project_id,
        provider="openrouter",
        name="Challenger",
        api_key=OPENROUTER_KEY,
    )

    service.update(
        database,
        credential_id=challenger.id,
        project_id=project_id,
        name=None,
        is_active=False,
    )

    promoted = service.update(
        database,
        credential_id=challenger.id,
        project_id=project_id,
        name=None,
        is_active=None,
        is_default=True,
    )

    database.refresh(current_default)

    assert promoted.is_default is True
    assert promoted.is_active is True
    assert current_default.is_default is False


def test_demoting_a_credential_leaves_it_active(
    database: Session,
    service: CredentialService,
    project_id: int,
) -> None:
    credential = service.create(
        database,
        project_id=project_id,
        provider="openrouter",
        name="Primary",
        api_key=OPENROUTER_KEY,
        is_default=True,
    )

    updated = service.update(
        database,
        credential_id=credential.id,
        project_id=project_id,
        name=None,
        is_active=None,
        is_default=False,
    )

    assert updated.is_default is False
    assert updated.is_active is True


def test_deactivating_a_credential_also_clears_the_default_flag(
    database: Session,
    service: CredentialService,
    project_id: int,
) -> None:
    credential = service.create(
        database,
        project_id=project_id,
        provider="openrouter",
        name="Primary",
        api_key=OPENROUTER_KEY,
        is_default=True,
    )

    updated = service.update(
        database,
        credential_id=credential.id,
        project_id=project_id,
        name=None,
        is_active=False,
    )

    assert updated.is_active is False
    assert updated.is_default is False


def test_update_without_changes_still_records_an_audit_entry(
    database: Session,
    service: CredentialService,
    project_id: int,
) -> None:
    credential = service.create(
        database,
        project_id=project_id,
        provider="openrouter",
        name="Primary",
        api_key=OPENROUTER_KEY,
    )

    service.update(
        database,
        credential_id=credential.id,
        project_id=project_id,
        name=None,
        is_active=None,
    )

    entries = database.scalars(
        select(CredentialAuditLog)
        .where(CredentialAuditLog.action == "updated")
        .where(CredentialAuditLog.project_id == project_id)
    ).all()

    assert len(entries) == 1
    assert entries[0].details == "No fields changed."


def test_update_cannot_reach_another_project(
    database: Session,
    service: CredentialService,
    project_id: int,
    other_project_id: int,
) -> None:
    credential = service.create(
        database,
        project_id=project_id,
        provider="openrouter",
        name="Primary",
        api_key=OPENROUTER_KEY,
    )

    with pytest.raises(CredentialNotFoundError):
        service.update(
            database,
            credential_id=credential.id,
            project_id=other_project_id,
            name="Hijacked",
            is_active=None,
        )


def test_update_rolls_back_when_the_commit_fails(
    database: Session,
    service: CredentialService,
    project_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = service.create(
        database,
        project_id=project_id,
        provider="openrouter",
        name="Primary",
        api_key=OPENROUTER_KEY,
    )

    def failing_commit(*args: object, **kwargs: object) -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(database, "commit", failing_commit)

    with pytest.raises(RuntimeError, match="commit failed"):
        service.update(
            database,
            credential_id=credential.id,
            project_id=project_id,
            name="Renamed",
            is_active=None,
        )

    monkeypatch.undo()

    assert (
        service.get_by_id(
            database,
            credential.id,
            project_id=project_id,
        ).name
        == "Primary"
    )


# ---------------------------------------------------------------------------
# Rotate, delete, decrypt
# ---------------------------------------------------------------------------


def test_rotate_replaces_the_secret_and_clears_the_test_result(
    database: Session,
    service: CredentialService,
    project_id: int,
) -> None:
    credential = service.create(
        database,
        project_id=project_id,
        provider="openrouter",
        name="Primary",
        api_key=OPENROUTER_KEY,
    )

    original_ciphertext = credential.encrypted_api_key
    replacement = "sk-or-v1-rotated000000000000000000000000"

    rotated = service.rotate(
        database,
        credential_id=credential.id,
        project_id=project_id,
        new_api_key=replacement,
    )

    assert rotated.encrypted_api_key != original_ciphertext
    assert rotated.masked_key.endswith(replacement[-4:])
    assert rotated.last_test_succeeded is None
    assert rotated.last_test_message is None
    assert rotated.last_tested_at is None

    _, plaintext = service.decrypt_api_key(
        database,
        credential_id=credential.id,
        project_id=project_id,
    )

    assert plaintext == replacement
    assert _audit_actions(database, project_id) == ["created", "rotated"]


def test_rotate_cannot_reach_another_project(
    database: Session,
    service: CredentialService,
    project_id: int,
    other_project_id: int,
) -> None:
    credential = service.create(
        database,
        project_id=project_id,
        provider="openrouter",
        name="Primary",
        api_key=OPENROUTER_KEY,
    )

    with pytest.raises(CredentialNotFoundError):
        service.rotate(
            database,
            credential_id=credential.id,
            project_id=other_project_id,
            new_api_key="sk-or-v1-attacker00000000000000000000",
        )


def test_delete_removes_the_secret_but_keeps_the_audit_trail(
    database: Session,
    service: CredentialService,
    project_id: int,
) -> None:
    credential = service.create(
        database,
        project_id=project_id,
        provider="openrouter",
        name="Primary",
        api_key=OPENROUTER_KEY,
    )
    credential_id = credential.id

    service.delete(
        database,
        credential_id=credential_id,
        project_id=project_id,
    )

    assert (
        database.scalar(
            select(ProviderCredential).where(ProviderCredential.id == credential_id)
        )
        is None
    )

    deletion_entries = database.scalars(
        select(CredentialAuditLog)
        .where(CredentialAuditLog.project_id == project_id)
        .where(CredentialAuditLog.action == "deleted")
    ).all()

    assert len(deletion_entries) == 1
    assert deletion_entries[0].credential_id is None
    assert OPENROUTER_KEY not in (deletion_entries[0].details or "")


def test_delete_cannot_reach_another_project(
    database: Session,
    service: CredentialService,
    project_id: int,
    other_project_id: int,
) -> None:
    credential = service.create(
        database,
        project_id=project_id,
        provider="openrouter",
        name="Primary",
        api_key=OPENROUTER_KEY,
    )

    with pytest.raises(CredentialNotFoundError):
        service.delete(
            database,
            credential_id=credential.id,
            project_id=other_project_id,
        )


def test_delete_rolls_back_when_the_commit_fails(
    database: Session,
    service: CredentialService,
    project_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = service.create(
        database,
        project_id=project_id,
        provider="openrouter",
        name="Primary",
        api_key=OPENROUTER_KEY,
    )

    def failing_commit(*args: object, **kwargs: object) -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(database, "commit", failing_commit)

    with pytest.raises(RuntimeError, match="commit failed"):
        service.delete(
            database,
            credential_id=credential.id,
            project_id=project_id,
        )

    monkeypatch.undo()

    assert (
        service.get_by_id(
            database,
            credential.id,
            project_id=project_id,
        ).id
        == credential.id
    )


def test_decrypt_round_trips_the_original_key(
    database: Session,
    service: CredentialService,
    project_id: int,
) -> None:
    credential = service.create(
        database,
        project_id=project_id,
        provider="openrouter",
        name="Primary",
        api_key=OPENROUTER_KEY,
    )

    returned, plaintext = service.decrypt_api_key(
        database,
        credential_id=credential.id,
        project_id=project_id,
    )

    assert returned.id == credential.id
    assert plaintext == OPENROUTER_KEY


def test_decrypt_refuses_an_inactive_credential_by_default(
    database: Session,
    service: CredentialService,
    project_id: int,
) -> None:
    credential = service.create(
        database,
        project_id=project_id,
        provider="openrouter",
        name="Primary",
        api_key=OPENROUTER_KEY,
    )

    service.update(
        database,
        credential_id=credential.id,
        project_id=project_id,
        name=None,
        is_active=False,
    )

    with pytest.raises(ValueError, match="inactive"):
        service.decrypt_api_key(
            database,
            credential_id=credential.id,
            project_id=project_id,
        )

    _, plaintext = service.decrypt_api_key(
        database,
        credential_id=credential.id,
        project_id=project_id,
        require_active=False,
    )

    assert plaintext == OPENROUTER_KEY


# ---------------------------------------------------------------------------
# Provider connectivity tests
# ---------------------------------------------------------------------------


@pytest.fixture
def openrouter_credential(
    database: Session,
    service: CredentialService,
    project_id: int,
) -> ProviderCredential:
    return service.create(
        database,
        project_id=project_id,
        provider="openrouter",
        name="Primary",
        api_key=OPENROUTER_KEY,
    )


@pytest.mark.parametrize(
    ("status_code", "expected_valid", "expected_message"),
    [
        (200, True, "OpenRouter accepted the credential."),
        (401, False, "OpenRouter rejected the credential."),
        (429, False, "OpenRouter rate limit was exceeded."),
        (500, False, "OpenRouter returned HTTP 500."),
    ],
)
async def test_openrouter_credential_test_maps_provider_responses(
    database: Session,
    service: CredentialService,
    project_id: int,
    openrouter_credential: ProviderCredential,
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    expected_valid: bool,
    expected_message: str,
) -> None:
    captured = install_mock_transport(
        monkeypatch,
        json_responder({"data": {}}, status_code=status_code),
    )

    credential, valid, message = await service.test_credential(
        database,
        credential_id=openrouter_credential.id,
        project_id=project_id,
    )

    assert valid is expected_valid
    assert message == expected_message
    assert credential.last_test_succeeded is expected_valid
    assert credential.last_tested_at is not None

    settings = get_settings()
    assert str(captured[0].url) == (f"{settings.openrouter_base_url.rstrip('/')}/key")
    assert captured[0].headers["authorization"] == (f"Bearer {OPENROUTER_KEY}")


@pytest.mark.parametrize(
    ("error", "expected_message"),
    [
        (
            httpx.ConnectTimeout("slow"),
            "OpenRouter credential test timed out.",
        ),
        (
            httpx.ConnectError("refused"),
            "Could not connect to OpenRouter.",
        ),
    ],
)
async def test_openrouter_credential_test_handles_transport_failures(
    database: Session,
    service: CredentialService,
    project_id: int,
    openrouter_credential: ProviderCredential,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected_message: str,
) -> None:
    install_mock_transport(monkeypatch, raising_responder(error))

    _, valid, message = await service.test_credential(
        database,
        credential_id=openrouter_credential.id,
        project_id=project_id,
    )

    assert valid is False
    assert message == expected_message


@pytest.mark.parametrize(
    ("status_code", "expected_valid", "expected_message"),
    [
        (200, True, "Groq accepted the credential."),
        (401, False, "Groq rejected the credential."),
        (429, False, "Groq rate limit was exceeded."),
        (503, False, "Groq returned HTTP 503."),
    ],
)
async def test_groq_credential_test_maps_provider_responses(
    database: Session,
    service: CredentialService,
    project_id: int,
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    expected_valid: bool,
    expected_message: str,
) -> None:
    credential = service.create(
        database,
        project_id=project_id,
        provider="groq",
        name="Groq Primary",
        api_key=GROQ_KEY,
    )

    captured = install_mock_transport(
        monkeypatch,
        json_responder({"data": []}, status_code=status_code),
    )

    _, valid, message = await service.test_credential(
        database,
        credential_id=credential.id,
        project_id=project_id,
    )

    assert valid is expected_valid
    assert message == expected_message
    assert str(captured[0].url).endswith("/models")


@pytest.mark.parametrize(
    ("error", "expected_message"),
    [
        (httpx.ReadTimeout("slow"), "Groq credential test timed out."),
        (httpx.ConnectError("refused"), "Could not connect to Groq."),
    ],
)
async def test_groq_credential_test_handles_transport_failures(
    database: Session,
    service: CredentialService,
    project_id: int,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected_message: str,
) -> None:
    credential = service.create(
        database,
        project_id=project_id,
        provider="groq",
        name="Groq Primary",
        api_key=GROQ_KEY,
    )

    install_mock_transport(monkeypatch, raising_responder(error))

    _, valid, message = await service.test_credential(
        database,
        credential_id=credential.id,
        project_id=project_id,
    )

    assert valid is False
    assert message == expected_message


async def test_credential_test_probes_other_providers(
    database: Session,
    service: CredentialService,
    project_id: int,
) -> None:
    credential = service.create(
        database,
        project_id=project_id,
        provider="ollama",
        name="Local",
        api_key="not-used-locally",
    )

    _, valid, message = await service.test_credential(
        database,
        credential_id=credential.id,
        project_id=project_id,
    )

    assert valid is False
    assert "HTTP 404" in message


async def test_credential_test_works_on_an_inactive_credential(
    database: Session,
    service: CredentialService,
    project_id: int,
    openrouter_credential: ProviderCredential,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.update(
        database,
        credential_id=openrouter_credential.id,
        project_id=project_id,
        name=None,
        is_active=False,
    )

    install_mock_transport(monkeypatch, json_responder({}, status_code=200))

    _, valid, _ = await service.test_credential(
        database,
        credential_id=openrouter_credential.id,
        project_id=project_id,
    )

    assert valid is True


async def test_credential_test_rolls_back_when_the_commit_fails(
    database: Session,
    service: CredentialService,
    project_id: int,
    openrouter_credential: ProviderCredential,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_mock_transport(monkeypatch, json_responder({}, status_code=200))

    def failing_commit(*args: object, **kwargs: object) -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(database, "commit", failing_commit)

    with pytest.raises(RuntimeError, match="commit failed"):
        await service.test_credential(
            database,
            credential_id=openrouter_credential.id,
            project_id=project_id,
        )


# ---------------------------------------------------------------------------
# Audit log queries
# ---------------------------------------------------------------------------


def test_audit_logs_can_be_filtered_by_credential(
    database: Session,
    service: CredentialService,
    project_id: int,
) -> None:
    first = service.create(
        database,
        project_id=project_id,
        provider="openrouter",
        name="First",
        api_key=OPENROUTER_KEY,
    )
    service.create(
        database,
        project_id=project_id,
        provider="openrouter",
        name="Second",
        api_key=OPENROUTER_KEY,
    )

    service.rotate(
        database,
        credential_id=first.id,
        project_id=project_id,
        new_api_key="sk-or-v1-rotated000000000000000000000000",
    )

    all_entries = service.list_audit_logs(
        database,
        project_id=project_id,
    )
    first_entries = service.list_audit_logs(
        database,
        project_id=project_id,
        credential_id=first.id,
    )

    assert len(all_entries) == 3
    assert {entry.action for entry in first_entries} == {
        "created",
        "rotated",
    }


def test_audit_logs_respect_the_limit_and_are_project_scoped(
    database: Session,
    service: CredentialService,
    project_id: int,
    other_project_id: int,
) -> None:
    service.create(
        database,
        project_id=other_project_id,
        provider="openrouter",
        name="Foreign",
        api_key=OPENROUTER_KEY,
    )

    for index in range(3):
        service.create(
            database,
            project_id=project_id,
            provider="openrouter",
            name=f"Key {index}",
            api_key=OPENROUTER_KEY,
        )

    limited = service.list_audit_logs(
        database,
        project_id=project_id,
        limit=2,
    )

    assert len(limited) == 2
    assert all(entry.project_id == project_id for entry in limited)
