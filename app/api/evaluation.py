from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.dependencies.admin_authentication import require_admin
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
    dependencies=[Depends(require_admin)],
)


@router.post(
    "/semantic-cache",
    response_model=SemanticEvaluationResponse,
)
def evaluate_semantic_cache(
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
        ge=0.001,
        le=0.25,
    ),
    include_pair_results: bool = Query(default=True),
) -> SemanticEvaluationResponse:
    if minimum_threshold > maximum_threshold:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=("minimum_threshold cannot be greater than maximum_threshold."),
        )

    count = int((maximum_threshold - minimum_threshold) / step + 1e-9) + 1
    if count > 201:
        raise HTTPException(
            status_code=400, detail="At most 201 thresholds are allowed."
        )
    thresholds = [round(minimum_threshold + index * step, 4) for index in range(count)]

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
