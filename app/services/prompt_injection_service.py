from __future__ import annotations

import base64
import re
import unicodedata
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
    _CONFUSABLES = str.maketrans(
        {
            "\u0430": "a",
            "\u0435": "e",
            "\u0456": "i",
            "\u043e": "o",
            "\u0440": "p",
            "\u0441": "c",
            "\u0445": "x",
            "\u0391": "A",
            "\u0392": "B",
            "\u0395": "E",
            "\u0399": "I",
            "\u039a": "K",
            "\u039c": "M",
            "\u039d": "N",
            "\u039f": "O",
            "\u03a1": "P",
            "\u03a4": "T",
            "\u03a7": "X",
        }
    )
    PATTERNS: tuple[tuple[str, str, int], ...] = (
        (
            r"\bignore\s*(all\s*)?previous\s*instructions\b",
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

    @classmethod
    def _normalize_text(cls, text: str) -> str:
        normalized = unicodedata.normalize("NFKC", text).translate(cls._CONFUSABLES)
        normalized = "".join(
            character
            for character in normalized
            if unicodedata.category(character) != "Cf"
        )
        # A common substitution uses capital I as a visual lowercase l in "all".
        normalized = re.sub(r"\ba[Ii][Ii]\b", "all", normalized)
        normalized = normalized.lower()
        # Collapse words split into individual characters: "i g n o r e".
        normalized = re.sub(
            r"(?<!\w)(?:[a-z]\s+){2,}[a-z](?!\w)",
            lambda match: re.sub(r"\s+", "", match.group(0)),
            normalized,
        )
        return " ".join(normalized.split())

    @classmethod
    def _decoded_base64_text(cls, text: str) -> str:
        decoded: list[str] = []
        for candidate in re.findall(
            r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{16,}={0,2}", text
        )[:20]:
            if len(candidate) % 4:
                continue
            try:
                raw = base64.b64decode(candidate, validate=True)
                value = raw.decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                continue
            if len(value) <= 4096 and all(
                character.isprintable() or character.isspace() for character in value
            ):
                decoded.append(value)
        return " ".join(decoded)

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

        normalized = self._normalize_text(text)
        decoded = self._decoded_base64_text(text)
        if decoded:
            normalized = f"{normalized} {self._normalize_text(decoded)}"

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
