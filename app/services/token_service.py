import re

from app.schemas.chat import ChatMessage


class TokenService:
    """
    Provides approximate token-counting functions.

    Later, this service can be upgraded with provider-specific tokenizers.
    """

    @staticmethod
    def estimate_text_tokens(text: str) -> int:
        if not text:
            return 0

        words_and_symbols = re.findall(
            r"\w+|[^\w\s]",
            text,
        )

        return max(
            1,
            round(len(words_and_symbols) / 0.75),
        )

    def estimate_message_tokens(
        self,
        messages: list[ChatMessage],
    ) -> int:
        total_tokens = 0

        for message in messages:
            # Approximate formatting overhead for each chat message.
            total_tokens += 4
            total_tokens += self.estimate_text_tokens(message.role)
            total_tokens += self.estimate_text_tokens(message.content)

        # Approximate overhead added before response generation.
        return total_tokens + 2
