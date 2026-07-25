from dataclasses import dataclass

from openai import AsyncOpenAI

from app.core.config import get_settings
from app.schemas.governance import (
    ModerationFinding,
)


@dataclass(frozen=True)
class ModerationResult:
    flagged: bool
    findings: list[ModerationFinding]


class ModerationService:
    def __init__(self) -> None:
        self.settings = get_settings()

    async def moderate(
        self,
        *,
        text: str,
        api_key: str,
    ) -> ModerationResult:
        client = AsyncOpenAI(api_key=api_key)

        response = await client.moderations.create(
            model=(self.settings.openai_moderation_model),
            input=text,
        )

        result = response.results[0]

        findings: list[ModerationFinding] = []

        categories = result.categories.model_dump()
        scores = result.category_scores.model_dump()

        for category, flagged in categories.items():
            findings.append(
                ModerationFinding(
                    category=category,
                    flagged=bool(flagged),
                    score=float(scores.get(category, 0.0)),
                )
            )

        return ModerationResult(
            flagged=bool(result.flagged),
            findings=findings,
        )
