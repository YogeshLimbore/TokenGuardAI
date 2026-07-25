import pytest

from app.services.semantic_evaluation_service import (
    SemanticEvaluationService,
)


@pytest.fixture(scope="module")
def evaluation_result():
    service = SemanticEvaluationService()

    return service.run_evaluation(
        thresholds=[
            0.85,
            0.90,
            0.92,
            0.94,
            0.96,
        ],
        include_pair_results=True,
    )


def test_evaluation_dataset_is_not_empty(
    evaluation_result,
):
    assert evaluation_result.dataset_size > 0


def test_dataset_contains_positive_examples(
    evaluation_result,
):
    assert evaluation_result.positive_examples > 0


def test_dataset_contains_negative_examples(
    evaluation_result,
):
    assert evaluation_result.negative_examples > 0


def test_recommended_threshold_exists(
    evaluation_result,
):
    assert evaluation_result.recommended_threshold is not None


def test_recommended_threshold_is_valid(
    evaluation_result,
):
    threshold = evaluation_result.recommended_threshold

    assert 0.0 <= threshold <= 1.0


def test_all_pair_results_have_valid_scores(
    evaluation_result,
):
    for pair_result in evaluation_result.pair_results:
        assert -1.0 <= pair_result.similarity_score <= 1.0


def test_recommended_threshold_has_no_false_positives(
    evaluation_result,
):
    threshold = evaluation_result.recommended_threshold

    threshold_result = next(
        result
        for result in (evaluation_result.threshold_results)
        if result.threshold == threshold
    )

    assert threshold_result.false_positives == 0
