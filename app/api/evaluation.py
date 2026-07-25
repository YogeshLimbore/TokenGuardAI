from fastapi import APIRouter, HTTPException, Query, status

from app.schemas.evaluation import (
    SemanticEvaluationResponse,
)
from app.services.semantic_evaluation_service import (
    EvaluationDatasetError,
    SemanticEvaluationService,
)

router = APIRouter(
    prefix="/v1/evaluation",
    tags=["Evaluation"],
)


@router.post(
    "/semantic-cache",
    response_model=SemanticEvaluationResponse,
)
async def evaluate_semantic_cache(
    minimum_threshold: float = Query(
        default=0.80,
        ge=0.0,
        le=1.0,
    ),
    maximum_threshold: float = Query(
        default=0.98,
        ge=0.0,
        le=1.0,
    ),
    step: float = Query(
        default=0.01,
        gt=0.0,
        le=0.25,
    ),
    include_pair_results: bool = Query(default=True),
) -> SemanticEvaluationResponse:
    if minimum_threshold > maximum_threshold:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=("minimum_threshold cannot be greater than maximum_threshold."),
        )

    thresholds: list[float] = []
    current_threshold = minimum_threshold

    while current_threshold <= maximum_threshold + 1e-9:
        thresholds.append(round(current_threshold, 4))
        current_threshold += step

    try:
        service = SemanticEvaluationService()

        return service.run_evaluation(
            thresholds=thresholds,
            include_pair_results=(include_pair_results),
        )

    except EvaluationDatasetError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(error),
        ) from error
