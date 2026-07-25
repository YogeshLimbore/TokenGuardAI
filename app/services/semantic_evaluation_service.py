from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from sklearn.metrics import (
    accuracy_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
)

from app.core.config import get_settings
from app.schemas.evaluation import (
    EvaluationPairResult,
    SemanticEvaluationResponse,
    ThresholdEvaluationResult,
)
from app.services.semantic_cache_service import (
    get_embedding_model,
)


class EvaluationDatasetError(RuntimeError):
    """Raised when the semantic evaluation dataset is invalid."""


@dataclass(frozen=True)
class EvaluationExample:
    id: str
    reference_prompt: str
    candidate_prompt: str
    should_reuse: bool
    category: str


class SemanticEvaluationService:
    def __init__(
        self,
        dataset_path: str = ("evaluation/semantic_cache_dataset.json"),
    ) -> None:
        self.settings = get_settings()
        self.dataset_path = Path(dataset_path)
        self.embedding_model = get_embedding_model()

    def load_dataset(
        self,
    ) -> list[EvaluationExample]:
        if not self.dataset_path.exists():
            raise EvaluationDatasetError(
                f"Evaluation dataset was not found: {self.dataset_path}"
            )

        try:
            with self.dataset_path.open(
                "r",
                encoding="utf-8",
            ) as file:
                raw_examples = json.load(file)
        except (OSError, json.JSONDecodeError) as error:
            raise EvaluationDatasetError(
                "The evaluation dataset is invalid."
            ) from error

        if not isinstance(raw_examples, list):
            raise EvaluationDatasetError("The evaluation dataset must be a JSON array.")

        examples: list[EvaluationExample] = []

        required_fields = {
            "id",
            "reference_prompt",
            "candidate_prompt",
            "should_reuse",
            "category",
        }

        for index, raw_example in enumerate(raw_examples):
            if not isinstance(raw_example, dict):
                raise EvaluationDatasetError(f"Example {index} must be an object.")

            missing_fields = required_fields - raw_example.keys()

            if missing_fields:
                raise EvaluationDatasetError(
                    f"Example {index} is missing: {sorted(missing_fields)}"
                )

            examples.append(
                EvaluationExample(
                    id=str(raw_example["id"]),
                    reference_prompt=str(raw_example["reference_prompt"]).strip(),
                    candidate_prompt=str(raw_example["candidate_prompt"]).strip(),
                    should_reuse=bool(raw_example["should_reuse"]),
                    category=str(raw_example["category"]).strip(),
                )
            )

        if not examples:
            raise EvaluationDatasetError("The evaluation dataset is empty.")

        return examples

    @staticmethod
    def _normalize_text(text: str) -> str:
        return " ".join(text.strip().lower().split())

    def calculate_similarity_scores(
        self,
        examples: list[EvaluationExample],
    ) -> list[float]:
        reference_prompts = [
            self._normalize_text(example.reference_prompt) for example in examples
        ]

        candidate_prompts = [
            self._normalize_text(example.candidate_prompt) for example in examples
        ]

        reference_embeddings = self.embedding_model.encode(
            reference_prompts,
            normalize_embeddings=True,
            convert_to_numpy=True,
        )

        candidate_embeddings = self.embedding_model.encode(
            candidate_prompts,
            normalize_embeddings=True,
            convert_to_numpy=True,
        )

        similarities = np.sum(
            reference_embeddings * candidate_embeddings,
            axis=1,
        )

        return [round(float(score), 6) for score in similarities]

    @staticmethod
    def _safe_divide(
        numerator: float,
        denominator: float,
    ) -> float:
        if denominator == 0:
            return 0.0

        return numerator / denominator

    def evaluate_threshold(
        self,
        *,
        threshold: float,
        labels: list[bool],
        similarities: list[float],
    ) -> ThresholdEvaluationResult:
        predictions = [similarity >= threshold for similarity in similarities]

        y_true = [int(label) for label in labels]

        y_predicted = [int(prediction) for prediction in predictions]

        matrix = confusion_matrix(
            y_true,
            y_predicted,
            labels=[0, 1],
        )

        true_negatives = int(matrix[0][0])
        false_positives = int(matrix[0][1])
        false_negatives = int(matrix[1][0])
        true_positives = int(matrix[1][1])

        negative_count = true_negatives + false_positives

        positive_count = true_positives + false_negatives

        return ThresholdEvaluationResult(
            threshold=round(threshold, 4),
            true_positives=true_positives,
            true_negatives=true_negatives,
            false_positives=false_positives,
            false_negatives=false_negatives,
            precision=round(
                precision_score(
                    y_true,
                    y_predicted,
                    zero_division=0,
                ),
                4,
            ),
            recall=round(
                recall_score(
                    y_true,
                    y_predicted,
                    zero_division=0,
                ),
                4,
            ),
            f1_score=round(
                f1_score(
                    y_true,
                    y_predicted,
                    zero_division=0,
                ),
                4,
            ),
            accuracy=round(
                accuracy_score(
                    y_true,
                    y_predicted,
                ),
                4,
            ),
            false_positive_rate=round(
                self._safe_divide(
                    false_positives,
                    negative_count,
                ),
                4,
            ),
            false_negative_rate=round(
                self._safe_divide(
                    false_negatives,
                    positive_count,
                ),
                4,
            ),
            safe_reuse_count=sum(y_true),
            predicted_reuse_count=sum(y_predicted),
        )

    @staticmethod
    def recommend_threshold(
        results: list[ThresholdEvaluationResult],
    ) -> tuple[float | None, str]:
        if not results:
            return (
                None,
                "No threshold results were available.",
            )

        zero_false_positive_results = [
            result for result in results if result.false_positives == 0
        ]

        if zero_false_positive_results:
            best_result = max(
                zero_false_positive_results,
                key=lambda result: (
                    result.recall,
                    result.f1_score,
                    -result.threshold,
                ),
            )

            return (
                best_result.threshold,
                (
                    "Selected the threshold with zero "
                    "false positives and the highest recall."
                ),
            )

        lowest_false_positive_count = min(result.false_positives for result in results)

        safest_results = [
            result
            for result in results
            if result.false_positives == lowest_false_positive_count
        ]

        best_result = max(
            safest_results,
            key=lambda result: (
                result.precision,
                result.f1_score,
                result.recall,
            ),
        )

        return (
            best_result.threshold,
            (
                "No threshold produced zero false positives. "
                "Selected the threshold with the fewest "
                "false positives and strongest precision."
            ),
        )

    def run_evaluation(
        self,
        *,
        thresholds: list[float] | None = None,
        include_pair_results: bool = True,
        progress_callback: (Callable[[float], None] | None) = None,
    ) -> SemanticEvaluationResponse:
        def report_progress(value: float) -> None:
            if progress_callback is not None:
                progress_callback(value)

        report_progress(5.0)

        examples = self.load_dataset()

        report_progress(20.0)

        if thresholds is None:
            thresholds = [
                round(value, 2)
                for value in np.arange(
                    0.80,
                    0.981,
                    0.01,
                )
            ]

        invalid_thresholds = [
            threshold for threshold in thresholds if not 0.0 <= threshold <= 1.0
        ]

        if invalid_thresholds:
            raise ValueError("Thresholds must be between 0 and 1.")

        similarities = self.calculate_similarity_scores(examples)

        report_progress(60.0)

        labels = [example.should_reuse for example in examples]

        threshold_results = [
            self.evaluate_threshold(
                threshold=threshold,
                labels=labels,
                similarities=similarities,
            )
            for threshold in thresholds
        ]

        report_progress(85.0)

        (
            recommended_threshold,
            recommendation_reason,
        ) = self.recommend_threshold(threshold_results)

        pair_results: list[EvaluationPairResult] = []

        if include_pair_results and recommended_threshold is not None:
            for example, similarity in zip(
                examples,
                similarities,
                strict=True,
            ):
                predicted_reuse = similarity >= recommended_threshold

                pair_results.append(
                    EvaluationPairResult(
                        id=example.id,
                        reference_prompt=(example.reference_prompt),
                        candidate_prompt=(example.candidate_prompt),
                        category=example.category,
                        expected_reuse=(example.should_reuse),
                        predicted_reuse=(predicted_reuse),
                        similarity_score=round(
                            similarity,
                            4,
                        ),
                        correct=(predicted_reuse == example.should_reuse),
                    )
                )

        report_progress(100.0)

        return SemanticEvaluationResponse(
            dataset_size=len(examples),
            positive_examples=sum(labels),
            negative_examples=(len(labels) - sum(labels)),
            model_name=(self.settings.semantic_cache_model),
            recommended_threshold=(recommended_threshold),
            recommendation_reason=(recommendation_reason),
            threshold_results=threshold_results,
            pair_results=pair_results,
        )
