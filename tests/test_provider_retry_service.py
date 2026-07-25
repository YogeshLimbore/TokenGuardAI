import httpx
import pytest

from app.core.config import Settings
from app.services import (
    provider_retry_service as provider_retry_service_module,
)
from app.services.provider_retry_service import (
    ProviderRetryService,
    is_retryable_provider_error,
)


class FakeHTTPStatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code} returned.")
        self.status_code = status_code


def _make_service(
    monkeypatch: pytest.MonkeyPatch,
    *,
    provider_retry_enabled: bool = True,
    provider_max_attempts: int = 3,
) -> ProviderRetryService:
    test_settings = Settings(
        provider_retry_enabled=provider_retry_enabled,
        provider_max_attempts=provider_max_attempts,
        # Zero-duration waits keep these tests fast; the exact
        # backoff timing is Tenacity's responsibility, not ours.
        provider_retry_min_wait_seconds=0.0,
        provider_retry_max_wait_seconds=0.0,
    )

    monkeypatch.setattr(
        provider_retry_service_module,
        "get_settings",
        lambda: test_settings,
    )

    return ProviderRetryService()


# --- is_retryable_provider_error ---------------------------------


def test_connection_timeout_is_retryable():
    assert is_retryable_provider_error(httpx.ReadTimeout("timed out"))


def test_connect_error_is_retryable():
    assert is_retryable_provider_error(httpx.ConnectError("could not connect"))


def test_http_429_is_retryable():
    assert is_retryable_provider_error(FakeHTTPStatusError(429))


def test_http_503_is_retryable():
    assert is_retryable_provider_error(FakeHTTPStatusError(503))


def test_rate_limit_message_is_retryable():
    assert is_retryable_provider_error(Exception("OpenRouter rate limit was exceeded."))


def test_invalid_api_key_is_not_retryable():
    assert not is_retryable_provider_error(
        Exception("OpenRouter rejected the API key.")
    )


def test_insufficient_credits_is_not_retryable():
    assert not is_retryable_provider_error(
        Exception("OpenRouter credits are insufficient.")
    )


def test_http_400_is_not_retryable():
    assert not is_retryable_provider_error(FakeHTTPStatusError(400))


def test_http_401_is_not_retryable():
    assert not is_retryable_provider_error(FakeHTTPStatusError(401))


# --- ProviderRetryService.execute --------------------------------


@pytest.mark.anyio
async def test_retries_until_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service(
        monkeypatch,
        provider_max_attempts=3,
    )

    call_count = 0

    async def operation() -> str:
        nonlocal call_count
        call_count += 1

        if call_count < 3:
            raise TimeoutError("request timed out")

        return "success"

    result = await service.execute(operation)

    assert result == "success"
    assert call_count == 3


@pytest.mark.anyio
async def test_stops_after_configured_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service(
        monkeypatch,
        provider_max_attempts=3,
    )

    call_count = 0

    async def operation() -> None:
        nonlocal call_count
        call_count += 1

        raise TimeoutError("request timed out")

    with pytest.raises(TimeoutError):
        await service.execute(operation)

    assert call_count == 3


@pytest.mark.anyio
async def test_does_not_retry_invalid_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service(
        monkeypatch,
        provider_max_attempts=3,
    )

    call_count = 0

    async def operation() -> None:
        nonlocal call_count
        call_count += 1

        raise PermissionError("OpenRouter rejected the API key.")

    with pytest.raises(PermissionError):
        await service.execute(operation)

    assert call_count == 1


@pytest.mark.anyio
async def test_disabled_retry_calls_operation_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _make_service(
        monkeypatch,
        provider_retry_enabled=False,
        provider_max_attempts=3,
    )

    call_count = 0

    async def operation() -> None:
        nonlocal call_count
        call_count += 1

        raise TimeoutError("request timed out")

    with pytest.raises(TimeoutError):
        await service.execute(operation)

    assert call_count == 1
