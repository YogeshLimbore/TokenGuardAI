CHARS_PER_TOKEN = 4


def count_tokens(text: str) -> int:
    """Rough token estimate until a real tokenizer is wired in."""
    if not text:
        return 0
    return max(1, len(text) // CHARS_PER_TOKEN)


def count_messages_tokens(messages: list[dict[str, str]]) -> int:
    return sum(count_tokens(message.get("content", "")) for message in messages)
