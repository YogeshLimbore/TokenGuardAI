from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

from app.core.config import get_settings

logger = logging.getLogger(__name__)

T = TypeVar("T")


class ProviderRetryExhaustedError(RuntimeError):
    pass


def is_retryable_provider_error(
    error: BaseException,
) -> bool:
    if isinstance(
        error,
        (
            httpx.ConnectError,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.PoolTimeout,
            asyncio.TimeoutError,
        ),
    ):
        return True

    status_code = getattr(
        error,
        "status_code",
        None,
    )

    if status_code in {
        408,
        409,
        425,
        429,
        500,
        502,
        503,
        504,
    }:
        return True

    message = str(error).lower()

    retryable_messages = (
        "temporarily unavailable",
        "connection reset",
        "connection refused",
        "could not connect",
        "timeout",
        "did not respond",
        "rate limit",
        "too many requests",
        "service unavailable",
    )

    return any(phrase in message for phrase in retryable_messages)


def log_retry(
    retry_state: RetryCallState,
) -> None:
    outcome = retry_state.outcome

    error = outcome.exception() if outcome is not None else None

    logger.warning(
        "Retrying provider request",
        extra={
            "attempt_number": (retry_state.attempt_number),
            "error_type": (type(error).__name__ if error else None),
            "error_message": (str(error) if error else None),
        },
    )


class ProviderRetryService:
    def __init__(self) -> None:
        self.settings = get_settings()

    async def execute(
        self,
        operation: Callable[
            [],
            Awaitable[T],
        ],
    ) -> T:
        if not self.settings.provider_retry_enabled:
            return await operation()

        retrying = AsyncRetrying(
            stop=stop_after_attempt(self.settings.provider_max_attempts),
            wait=wait_random_exponential(
                min=(self.settings.provider_retry_min_wait_seconds),
                max=(self.settings.provider_retry_max_wait_seconds),
            ),
            retry=retry_if_exception(is_retryable_provider_error),
            before_sleep=log_retry,
            reraise=True,
        )

        async for attempt in retrying:
            with attempt:
                return await operation()

        raise ProviderRetryExhaustedError("Provider retry attempts exhausted.")
