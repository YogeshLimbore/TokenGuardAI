import pytest

from app.schemas.chat import ChatMessage
from app.services.governance_service import GovernanceService


@pytest.mark.anyio
async def test_missing_moderation_key_fails_closed(monkeypatch):
    service = GovernanceService()
    monkeypatch.setattr(service.settings, "input_moderation_enabled", True)
    monkeypatch.setattr(service.settings, "output_moderation_enabled", True)
    monkeypatch.setattr(service.settings, "moderation_fail_closed", True)
    monkeypatch.setattr(service.settings, "openai_api_key", None)

    input_result = await service.evaluate_input_moderation(
        messages=[ChatMessage(role="user", content="ordinary text")]
    )
    output_result = await service.evaluate_output(text="ordinary response")

    assert input_result.safe is False
    assert output_result.safe is False
    assert input_result.event_type == "moderation_unavailable"
    assert output_result.event_type == "moderation_unavailable"
    assert input_result.detected_categories == ["moderation_not_configured"]


@pytest.mark.anyio
async def test_missing_moderation_key_can_explicitly_fail_open(monkeypatch):
    service = GovernanceService()
    monkeypatch.setattr(service.settings, "input_moderation_enabled", True)
    monkeypatch.setattr(service.settings, "moderation_fail_closed", False)
    monkeypatch.setattr(service.settings, "openai_api_key", None)

    result = await service.evaluate_input_moderation(
        messages=[ChatMessage(role="user", content="ordinary text")]
    )

    assert result.safe is True
    assert result.event_type == "moderation_unavailable"
