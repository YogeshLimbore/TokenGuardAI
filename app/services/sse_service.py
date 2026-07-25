import json
from typing import Any


class SSEService:
    """
    Formats data as Server-Sent Event (SSE) wire messages.

    SSE messages use ``event``, ``data``, ``id``, and ``retry``
    fields, each on their own line, separated from the next
    message by a blank line.
    """

    @staticmethod
    def encode(
        *,
        event: str,
        data: dict[str, Any],
        event_id: str | None = None,
        retry_ms: int | None = None,
    ) -> str:
        lines: list[str] = []

        if event_id is not None:
            lines.append(f"id: {event_id}")

        lines.append(f"event: {event}")

        if retry_ms is not None:
            lines.append(f"retry: {retry_ms}")

        serialized = json.dumps(
            data,
            ensure_ascii=False,
            separators=(",", ":"),
        )

        for line in serialized.splitlines():
            lines.append(f"data: {line}")

        return "\n".join(lines) + "\n\n"

    @staticmethod
    def heartbeat() -> str:
        return ": heartbeat\n\n"
