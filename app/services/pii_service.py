from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property, lru_cache
from typing import Any, cast

from presidio_analyzer import (
    AnalyzerEngine,
    RecognizerResult,
)
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_anonymizer import (
    AnonymizerEngine,
)
from presidio_anonymizer.entities import (
    OperatorConfig,
)

from app.core.config import get_settings
from app.schemas.governance import (
    SensitiveDataFinding,
)


@dataclass(frozen=True)
class PIIScanResult:
    original_text: str
    processed_text: str
    sanitized: bool
    blocked: bool
    findings: list[SensitiveDataFinding]


@lru_cache
def _get_analyzer_engine() -> AnalyzerEngine:
    # Presidio's analyzer loads an NLP model (e.g. spaCy) at
    # construction time, which is expensive. GovernanceService
    # builds a fresh PIIService per request, so this must be
    # cached at module scope rather than re-created every call.
    nlp_engine = NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
        }
    ).create_engine()
    return AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=["en"])


@lru_cache
def _get_anonymizer_engine() -> AnonymizerEngine:
    return AnonymizerEngine()


class PIIService:
    def __init__(self) -> None:
        self.settings = get_settings()

    @cached_property
    def analyzer(self) -> AnalyzerEngine:
        return _get_analyzer_engine()

    @cached_property
    def anonymizer(self) -> AnonymizerEngine:
        return _get_anonymizer_engine()

    def scan(
        self,
        *,
        text: str,
        entity_actions: dict[str, str],
        allowed_entities: set[str],
        entities: list[str],
    ) -> PIIScanResult:
        if not self.settings.pii_detection_enabled:
            return PIIScanResult(
                original_text=text,
                processed_text=text,
                sanitized=False,
                blocked=False,
                findings=[],
            )

        analyzer_results = self.analyzer.analyze(
            text=text,
            language="en",
            entities=entities,
            score_threshold=(self.settings.pii_score_threshold),
        )

        findings: list[SensitiveDataFinding] = []
        redaction_results: list[RecognizerResult] = []
        blocked = False

        for result in analyzer_results:
            entity_type = result.entity_type

            if entity_type in allowed_entities:
                continue

            action = entity_actions.get(
                entity_type,
                self.settings.pii_default_action,
            )

            findings.append(
                SensitiveDataFinding(
                    entity_type=entity_type,
                    score=float(result.score),
                    start=result.start,
                    end=result.end,
                    action=action,
                )
            )

            if action == "block":
                blocked = True

            elif action == "redact":
                redaction_results.append(result)

        processed_text = text

        if redaction_results:
            anonymized = self.anonymizer.anonymize(
                text=text,
                # presidio-analyzer and presidio-anonymizer each ship their
                # own RecognizerResult class; the anonymizer is documented to
                # accept the analyzer's results despite the distinct types.
                analyzer_results=cast(
                    "list[Any]",
                    redaction_results,
                ),
                operators={
                    "DEFAULT": OperatorConfig(
                        "replace",
                        {"new_value": ("<REDACTED>")},
                    )
                },
            )

            processed_text = anonymized.text

        return PIIScanResult(
            original_text=text,
            processed_text=processed_text,
            sanitized=(processed_text != text),
            blocked=blocked,
            findings=findings,
        )
