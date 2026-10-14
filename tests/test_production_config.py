"""Production configuration guardrail tests."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_production_rejects_debug_true():
    with pytest.raises(ValidationError) as error:
        Settings(
            environment="production",
            debug=True,
            jwt_secret_key=("production-jwt-secret-key-that-is-long-enough-1234567890"),
            token_guard_master_key="master-key-for-tests",
            bootstrap_admin_password=None,
            allow_public_registration=False,
        )

    assert "DEBUG must be false" in str(error.value)


def test_production_rejects_placeholder_jwt_secret():
    with pytest.raises(ValidationError) as error:
        Settings(
            environment="production",
            debug=False,
            jwt_secret_key=(
                "change_me_replace_with_long_random_secret_at_least_48_chars"
            ),
            token_guard_master_key="master-key-for-tests",
            bootstrap_admin_password=None,
            allow_public_registration=False,
        )

    assert "JWT_SECRET_KEY" in str(error.value)


def test_production_accepts_hardened_settings():
    settings = Settings(
        environment="production",
        debug=False,
        jwt_secret_key=("production-jwt-secret-key-that-is-long-enough-1234567890"),
        token_guard_master_key="master-key-for-tests",
        bootstrap_admin_password=None,
        allow_public_registration=False,
        enable_docs=False,
    )

    assert settings.docs_enabled is False
    assert settings.environment == "production"


def test_development_keeps_docs_enabled_by_default():
    settings = Settings(environment="development", debug=True)
    assert settings.docs_enabled is True


@pytest.mark.parametrize(
    "override, expected",
    [
        ({"budget_enforcement_enabled": False}, "Budget and policy enforcement"),
        ({"policy_enforcement_enabled": False}, "Budget and policy enforcement"),
        ({"rate_limiting_enabled": False}, "Rate limiting"),
        ({"input_moderation_enabled": True}, "OPENAI_API_KEY"),
        ({"output_moderation_enabled": True}, "OPENAI_API_KEY"),
    ],
)
def test_production_rejects_disabled_safety_controls(override, expected):
    values = {
        "environment": "production",
        "debug": False,
        "jwt_secret_key": "production-jwt-secret-key-that-is-long-enough-1234567890",
        "token_guard_master_key": "master-key-for-tests",
        "bootstrap_admin_password": None,
        "allow_public_registration": False,
        **override,
    }
    with pytest.raises(ValidationError) as error:
        Settings(**values)

    assert expected in str(error.value)
