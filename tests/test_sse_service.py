from app.services.sse_service import SSEService


def test_encode_includes_event_and_data_lines():
    message = SSEService.encode(
        event="token",
        data={"type": "token", "content": "hi"},
    )

    lines = message.split("\n")

    assert lines[0] == "event: token"
    assert lines[1] == 'data: {"type":"token","content":"hi"}'
    assert message.endswith("\n\n")


def test_encode_includes_optional_id_and_retry():
    message = SSEService.encode(
        event="metadata",
        data={"request_id": "abc-123"},
        event_id="abc-123",
        retry_ms=2000,
    )

    lines = message.split("\n")

    assert lines[0] == "id: abc-123"
    assert lines[1] == "event: metadata"
    assert lines[2] == "retry: 2000"
    assert lines[3] == 'data: {"request_id":"abc-123"}'


def test_encode_omits_id_when_not_provided():
    message = SSEService.encode(
        event="usage",
        data={"input_tokens": 1},
    )

    assert not message.startswith("id:")
    assert message.startswith("event: usage")


def test_encode_serializes_multiline_data_as_multiple_data_lines():
    # Even though our payloads are single-line JSON today, the SSE
    # spec requires every line of a multi-line data value to be
    # prefixed with its own "data:" field.
    message = SSEService.encode(
        event="token",
        data={"content": "line one"},
    )

    data_lines = [line for line in message.split("\n") if line.startswith("data:")]

    assert len(data_lines) == 1


def test_encode_ends_with_blank_line_separator():
    message = SSEService.encode(
        event="completed",
        data={"request_id": "abc"},
    )

    # SSE messages are separated by a blank line.
    assert message.endswith("\n\n")
    assert not message.endswith("\n\n\n")


def test_heartbeat_is_a_comment_line():
    heartbeat = SSEService.heartbeat()

    assert heartbeat.startswith(":")
    assert heartbeat.endswith("\n\n")
