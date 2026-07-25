from datetime import UTC

from app.services.project_key_service import (
    ProjectKeyService,
)


def test_generated_key_has_expected_prefix():
    service = ProjectKeyService()

    key = service.generate_key()

    assert key.startswith(service.settings.project_api_key_prefix)


def test_key_hash_is_deterministic():
    key = "tg_live_example"

    first_hash = ProjectKeyService.hash_key(key)
    second_hash = ProjectKeyService.hash_key(key)

    assert first_hash == second_hash


def test_different_keys_have_different_hashes():
    first_hash = ProjectKeyService.hash_key("tg_live_first")

    second_hash = ProjectKeyService.hash_key("tg_live_second")

    assert first_hash != second_hash


def test_mask_does_not_reveal_complete_key():
    key = "tg_live_abcdefghijklmnopqrstuvwxyz"

    masked = ProjectKeyService.mask_key(key)

    assert masked != key
    assert key not in masked
    assert masked.startswith("tg_live_")


def test_is_expired_returns_false_for_none():
    assert ProjectKeyService.is_expired(None) is False


def test_is_expired_returns_true_for_past_datetime():
    from datetime import datetime, timedelta

    past = datetime.now(UTC) - timedelta(minutes=5)

    assert ProjectKeyService.is_expired(past) is True


def test_is_expired_returns_false_for_future_datetime():
    from datetime import datetime, timedelta

    future = datetime.now(UTC) + timedelta(minutes=5)

    assert ProjectKeyService.is_expired(future) is False
