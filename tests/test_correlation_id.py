import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.middleware.correlation_id import (
    CORRELATION_HEADER,
)
from app.observability.context import (
    get_correlation_id,
    reset_correlation_id,
    set_correlation_id,
)


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_supplied_correlation_id_is_preserved(
    client: TestClient,
) -> None:
    response = client.get(
        "/health",
        headers={CORRELATION_HEADER: "test-correlation-001"},
    )

    assert response.status_code == 200
    assert response.headers[CORRELATION_HEADER] == "test-correlation-001"


def test_missing_correlation_id_is_generated(
    client: TestClient,
) -> None:
    response = client.get("/health")

    assert response.status_code == 200

    generated_id = response.headers[CORRELATION_HEADER]

    assert generated_id

    # The generated value should be a valid UUID4 string.
    parsed = uuid.UUID(generated_id)
    assert parsed.version == 4


def test_response_includes_same_id_as_request(
    client: TestClient,
) -> None:
    supplied_id = str(uuid.uuid4())

    response = client.get(
        "/health",
        headers={CORRELATION_HEADER: supplied_id},
    )

    assert response.headers[CORRELATION_HEADER] == supplied_id


def test_two_requests_receive_different_generated_ids(
    client: TestClient,
) -> None:
    first_response = client.get("/health")
    second_response = client.get("/health")

    first_id = first_response.headers[CORRELATION_HEADER]
    second_id = second_response.headers[CORRELATION_HEADER]

    assert first_id != second_id


@pytest.mark.anyio
async def test_concurrent_requests_do_not_share_ids() -> None:
    """
    ``ContextVar`` isolates state per asyncio task. This test bypasses
    the HTTP layer and exercises the context helpers directly to
    confirm that concurrently running "requests" never observe each
    other's correlation id, mirroring what the middleware relies on
    when many requests are in flight at once.
    """

    observed: dict[str, str | None] = {}

    async def simulate_request(name: str, correlation_id: str) -> None:
        token = set_correlation_id(correlation_id)

        try:
            # Yield control so the other simulated request can run
            # and potentially interleave with this one.
            await asyncio.sleep(0.05)
            observed[name] = get_correlation_id()
        finally:
            reset_correlation_id(token)

    await asyncio.gather(
        simulate_request("first", "correlation-aaa"),
        simulate_request("second", "correlation-bbb"),
    )

    assert observed["first"] == "correlation-aaa"
    assert observed["second"] == "correlation-bbb"

    # Context set inside the tasks must not leak back to the
    # enclosing task once both have finished.
    assert get_correlation_id() is None
