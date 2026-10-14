from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from app.core.config import get_settings
from app.schemas.chat import ChatCompletionRequest


class RoutingConfigurationError(RuntimeError):
    pass


class RoutingRequestError(ValueError):
    """The caller omitted or supplied an invalid manual routing value."""


@dataclass(frozen=True)
class RouteTarget:
    tier: str
    provider: str
    model: str
    maximum_output_tokens: int
    description: str


@dataclass(frozen=True)
class InternalRoutingDecision:
    automatic_routing_used: bool
    tier: str
    provider: str
    model: str
    maximum_output_tokens: int | None
    complexity_score: int
    confidence: float
    reasons: list[str]
    original_provider: str | None
    original_model: str | None


class RoutingService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.policy = self._load_policy()

    def _load_policy(self) -> dict[str, object]:
        policy_path = Path(self.settings.routing_policy_file)

        if not policy_path.exists():
            raise RoutingConfigurationError(f"Routing policy not found: {policy_path}")

        try:
            with policy_path.open(
                "r",
                encoding="utf-8",
            ) as file:
                policy = json.load(file)
        except (OSError, json.JSONDecodeError) as error:
            raise RoutingConfigurationError("Routing policy is invalid.") from error

        if not isinstance(policy, dict):
            raise RoutingConfigurationError("Routing policy must be a JSON object.")

        return policy

    @staticmethod
    def _combined_user_text(
        request: ChatCompletionRequest,
    ) -> str:
        return "\n".join(
            message.content for message in request.messages if message.role == "user"
        )

    @staticmethod
    def _word_count(text: str) -> int:
        return len(re.findall(r"\b\w+\b", text))

    @staticmethod
    def _contains_any(
        text: str,
        terms: list[str],
    ) -> list[str]:
        lowered_text = text.lower()

        return [term for term in terms if term in lowered_text]

    def _calculate_complexity(
        self,
        request: ChatCompletionRequest,
    ) -> tuple[int, list[str]]:
        text = self._combined_user_text(request)
        word_count = self._word_count(text)

        score = 0
        reasons: list[str] = []

        if word_count > 50:
            score += 1
            reasons.append("The prompt contains more than 50 words.")

        if word_count > 150:
            score += 2
            reasons.append("The prompt contains more than 150 words.")

        if len(request.messages) >= 6:
            score += 1
            reasons.append("The conversation has substantial history.")

        advanced_terms = self._contains_any(
            text,
            [
                "architecture",
                "system design",
                "distributed",
                "production-grade",
                "optimize",
                "debug",
                "root cause",
                "trade-off",
                "tradeoff",
                "security analysis",
                "threat model",
                "algorithm",
                "mathematical proof",
                "reason step by step",
                "deep analysis",
            ],
        )

        if advanced_terms:
            score += min(
                len(advanced_terms) * 2,
                6,
            )
            reasons.append("Advanced task indicators: " + ", ".join(advanced_terms[:4]))

        coding_terms = self._contains_any(
            text,
            [
                "write code",
                "implement",
                "refactor",
                "traceback",
                "exception",
                "python",
                "javascript",
                "sql",
                "fastapi",
                "docker",
                "kubernetes",
            ],
        )

        if coding_terms:
            score += min(
                len(coding_terms),
                4,
            )
            reasons.append("Coding indicators: " + ", ".join(coding_terms[:4]))

        simple_terms = self._contains_any(
            text,
            [
                "correct grammar",
                "rewrite this sentence",
                "one sentence",
                "one word",
                "classify",
                "extract",
                "translate",
                "short answer",
            ],
        )

        if simple_terms:
            score -= 2
            reasons.append("Simple-task indicators: " + ", ".join(simple_terms[:3]))

        if request.max_tokens is not None:
            if request.max_tokens >= 1500:
                score += 2
                reasons.append("A long output was requested.")
            elif request.max_tokens <= 200:
                score -= 1
                reasons.append("A short output limit was requested.")

        return max(score, 0), reasons

    def _get_route(
        self,
        tier: str,
    ) -> RouteTarget:
        routes = self.policy.get("routes")

        if not isinstance(routes, dict):
            raise RoutingConfigurationError("Routing policy has no routes object.")

        route = routes.get(tier)

        if not isinstance(route, dict):
            raise RoutingConfigurationError(f"Route '{tier}' is missing.")

        try:
            return RouteTarget(
                tier=tier,
                provider=str(route["provider"]),
                model=str(route["model"]),
                maximum_output_tokens=int(route["maximum_output_tokens"]),
                description=str(
                    route.get(
                        "description",
                        "",
                    )
                ),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RoutingConfigurationError(f"Route '{tier}' is invalid.") from error

    def _choose_tier(
        self,
        *,
        score: int,
        preference: str,
    ) -> str:
        thresholds = self.policy.get(
            "thresholds",
            {},
        )

        if not isinstance(thresholds, dict):
            thresholds = {}

        balanced_score = int(
            thresholds.get(
                "balanced_score",
                3,
            )
        )

        advanced_score = int(
            thresholds.get(
                "advanced_score",
                7,
            )
        )

        if preference == "cost":
            advanced_score += 3
            balanced_score += 1

        elif preference == "quality":
            advanced_score = max(
                advanced_score - 2,
                balanced_score,
            )
            balanced_score = max(
                balanced_score - 1,
                1,
            )

        if score >= advanced_score:
            return "advanced"

        if score >= balanced_score:
            return "balanced"

        return "simple"

    @staticmethod
    def _confidence(
        score: int,
        tier: str,
    ) -> float:
        if tier == "simple":
            distance = max(3 - score, 0)
        elif tier == "balanced":
            distance = min(
                abs(score - 3),
                abs(7 - score),
            )
        else:
            distance = max(score - 7, 0)

        return round(
            min(0.65 + distance * 0.05, 0.95),
            2,
        )

    def decide(
        self,
        request: ChatCompletionRequest,
    ) -> InternalRoutingDecision:
        original_provider = request.provider
        original_model = request.model

        routing_enabled = self.settings.auto_routing_enabled and request.auto_route

        if not routing_enabled:
            selected_model = request.model

            if selected_model is None:
                from app.providers.registry import get_provider

                selected_model = get_provider(request.provider).default_model
                if not selected_model:
                    raise RoutingRequestError(
                        "Specify a model for the selected provider."
                    )

            return InternalRoutingDecision(
                automatic_routing_used=False,
                tier="manual",
                provider=request.provider,
                model=selected_model,
                maximum_output_tokens=None,
                complexity_score=0,
                confidence=1.0,
                reasons=["Automatic routing was disabled."],
                original_provider=original_provider,
                original_model=original_model,
            )

        score, reasons = self._calculate_complexity(request)

        tier = self._choose_tier(
            score=score,
            preference=request.routing_preference,
        )

        route = self._get_route(tier)

        reasons.append(
            f"Selected the {tier} route using the "
            f"'{request.routing_preference}' preference."
        )

        return InternalRoutingDecision(
            automatic_routing_used=True,
            tier=tier,
            provider=route.provider,
            model=route.model,
            maximum_output_tokens=(route.maximum_output_tokens),
            complexity_score=score,
            confidence=self._confidence(
                score,
                tier,
            ),
            reasons=reasons,
            original_provider=original_provider,
            original_model=original_model,
        )
