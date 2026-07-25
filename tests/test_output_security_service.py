from app.services.output_security_service import (
    OutputSecurityService,
)


def test_ordinary_text_is_safe():
    service = OutputSecurityService()

    result = service.analyze("The weather today is sunny with a high of 75F.")

    assert result.safe is True
    assert result.findings == []


def test_openai_style_key_is_detected():
    service = OutputSecurityService()

    result = service.analyze("Here is your key: sk-abcdefghijklmnopqrstuvwx1234")

    assert result.safe is False
    assert "openai_style_key" in result.findings


def test_tokenguard_project_key_is_detected():
    service = OutputSecurityService()

    result = service.analyze(
        "Sure, use tg_live_abcdefghijklmnopqrstuvwxyz012345 to authenticate."
    )

    assert result.safe is False
    assert "tokenguard_project_key" in result.findings


def test_tokenguard_admin_key_is_detected():
    service = OutputSecurityService()

    result = service.analyze(
        "The admin key is tg_admin_abcdefghijklmnopqrstuvwxyz012345."
    )

    assert result.safe is False
    assert "tokenguard_admin_key" in result.findings


def test_bearer_token_is_detected():
    service = OutputSecurityService()

    result = service.analyze(
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789"
    )

    assert result.safe is False
    assert "bearer_token" in result.findings


def test_private_key_block_is_detected():
    service = OutputSecurityService()

    result = service.analyze(
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIBogIBAAKCAQEAtotallyfakekeymaterial\n"
        "-----END RSA PRIVATE KEY-----"
    )

    assert result.safe is False
    assert "private_key" in result.findings


def test_multiple_findings_are_all_reported():
    text = (
        "key sk-abcdefghijklmnopqrstuvwx1234 and "
        "tg_admin_abcdefghijklmnopqrstuvwxyz012345"
    )

    service = OutputSecurityService()
    result = service.analyze(text)

    assert result.safe is False
    assert set(result.findings) == {
        "openai_style_key",
        "tokenguard_admin_key",
    }
