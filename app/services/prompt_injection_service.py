from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.config import get_settings
from app.schemas.governance import (
    PromptInjectionFinding,
)


@dataclass(frozen=True)
class PromptInjectionResult:
    score: int
    blocked: bool
    review_required: bool
    findings: list[PromptInjectionFinding]


class PromptInjectionService:
    PATTERNS: tuple[tuple[str, str, int], ...] = (
        (
            r"\bignore\s+(all\s+)?previous\s+instructions\b",
            "instruction_override",
            45,
        ),
        (
            r"\bignore\s+(the\s+)?system\s+prompt\b",
            "instruction_override",
            50,
        ),
        (
            r"\breveal\s+(the\s+)?system\s+prompt\b",
            "system_prompt_extraction",
            55,
        ),
        (
            r"\bshow\s+(me\s+)?hidden\s+instructions\b",
            "system_prompt_extraction",
            55,
        ),
        (
            r"\bdeveloper\s+message\b",
            "instruction_extraction",
            25,
        ),
        (
            r"\bprint\s+(your\s+)?api\s+key\b",
            "credential_extraction",
            65,
        ),
        (
            r"\bexpose\s+(stored\s+)?credentials\b",
            "credential_extraction",
            65,
        ),
        (
            r"\bact\s+as\s+the\s+system\b",
            "role_manipulation",
            35,
        ),
        (
            r"\byou\s+are\s+now\s+(in\s+)?developer\s+mode\b",
            "role_manipulation",
            40,
        ),
        (
            r"\bdo\s+not\s+follow\s+your\s+safety\b",
            "safety_bypass",
            40,
        ),
        (
            r"\bdecode\s+this\s+base64\b",
            "encoded_instruction",
            15,
        ),
        (
            r"\bexecute\s+the\s+following\s+hidden\s+command\b",
            "hidden_instruction",
            45,
        ),
    )

    def __init__(self) -> None:
        self.settings = get_settings()

    def analyze(
        self,
        text: str,
    ) -> PromptInjectionResult:
        if not self.settings.prompt_injection_detection_enabled:
            return PromptInjectionResult(
                score=0,
                blocked=False,
                review_required=False,
                findings=[],
            )

        normalized = " ".join(text.lower().split())

        findings: list[PromptInjectionFinding] = []

        total_score = 0

        for pattern, category, score in self.PATTERNS:
            match = re.search(
                pattern,
                normalized,
                flags=re.IGNORECASE,
            )

            if not match:
                continue

            total_score += score

            findings.append(
                PromptInjectionFinding(
                    category=category,
                    description=(f"Potential prompt-injection indicator: {category}."),
                    score=score,
                    matched_pattern=pattern,
                )
            )

        if len(text) > 20_000:
            total_score += 10

            findings.append(
                PromptInjectionFinding(
                    category="oversized_prompt",
                    description=("The prompt is unusually large."),
                    score=10,
                )
            )

        total_score = min(total_score, 100)

        return PromptInjectionResult(
            score=total_score,
            blocked=(total_score >= self.settings.prompt_injection_block_threshold),
            review_required=(
                self.settings.prompt_injection_review_threshold
                <= total_score
                < self.settings.prompt_injection_block_threshold
            ),
            findings=findings,
        )
