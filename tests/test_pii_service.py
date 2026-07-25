from app.core.config import get_settings
from app.services.pii_service import PIIService

ENTITIES = [
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "CREDIT_CARD",
    "PERSON",
    "LOCATION",
]


def test_email_address_is_redacted():
    service = PIIService()

    result = service.scan(
        text=("Please follow up with john.smith@example.com about the invoice."),
        entities=ENTITIES,
        allowed_entities=set(),
        entity_actions={"EMAIL_ADDRESS": "redact"},
    )

    assert result.sanitized is True
    assert result.blocked is False
    assert "john.smith@example.com" not in result.processed_text
    assert "<REDACTED>" in result.processed_text
    assert any(
        finding.entity_type == "EMAIL_ADDRESS" and finding.action == "redact"
        for finding in result.findings
    )


def test_phone_number_is_redacted():
    service = PIIService()

    result = service.scan(
        text="My phone number is 415-555-0132, call anytime.",
        entities=ENTITIES,
        allowed_entities=set(),
        entity_actions={"PHONE_NUMBER": "redact"},
    )

    assert result.sanitized is True
    assert "415-555-0132" not in result.processed_text
    assert any(finding.entity_type == "PHONE_NUMBER" for finding in result.findings)


def test_credit_card_is_blocked_instead_of_redacted():
    service = PIIService()

    result = service.scan(
        text=("My card number is 4111111111111111, please keep it on file."),
        entities=ENTITIES,
        allowed_entities=set(),
        entity_actions={"CREDIT_CARD": "block"},
    )

    assert result.blocked is True

    finding = next(
        finding for finding in result.findings if finding.entity_type == "CREDIT_CARD"
    )
    assert finding.action == "block"


def test_allowlisted_entity_is_left_untouched():
    service = PIIService()

    text = "Hi, I am John Smith from Paris."

    result = service.scan(
        text=text,
        entities=ENTITIES,
        allowed_entities={"PERSON", "LOCATION"},
        entity_actions={
            "PERSON": "redact",
            "LOCATION": "redact",
        },
    )

    assert result.sanitized is False
    assert result.blocked is False
    assert result.processed_text == text
    assert result.findings == []


def test_unconfigured_entity_falls_back_to_default_action():
    settings = get_settings()
    original_default = settings.pii_default_action
    settings.pii_default_action = "redact"

    try:
        service = PIIService()

        result = service.scan(
            text="Email me at jane.doe@example.com please.",
            entities=ENTITIES,
            allowed_entities=set(),
            # No explicit action configured for EMAIL_ADDRESS.
            entity_actions={},
        )

        assert result.sanitized is True
        assert "jane.doe@example.com" not in result.processed_text
    finally:
        settings.pii_default_action = original_default


def test_disabled_detection_returns_original_text_unmodified():
    settings = get_settings()
    original = settings.pii_detection_enabled
    settings.pii_detection_enabled = False

    try:
        service = PIIService()

        text = "Email me at jane.doe@example.com please."
        result = service.scan(
            text=text,
            entities=ENTITIES,
            allowed_entities=set(),
            entity_actions={"EMAIL_ADDRESS": "redact"},
        )

        assert result.processed_text == text
        assert result.sanitized is False
        assert result.findings == []
    finally:
        settings.pii_detection_enabled = original
