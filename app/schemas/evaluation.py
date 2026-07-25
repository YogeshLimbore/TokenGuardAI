from pydantic import BaseModel, Field


class EvaluationPairResult(BaseModel):
    id: str
    reference_prompt: str
    candidate_prompt: str
    category: str
    expected_reuse: bool
    predicted_reuse: bool
    similarity_score: float
    correct: bool


class ThresholdEvaluationResult(BaseModel):
    threshold: float

    true_positives: int
    true_negatives: int
    false_positives: int
    false_negatives: int

    precision: float
    recall: float
    f1_score: float
    accuracy: float
    false_positive_rate: float
    false_negative_rate: float

    safe_reuse_count: int
    predicted_reuse_count: int


class SemanticEvaluationResponse(BaseModel):
    dataset_size: int
    positive_examples: int
    negative_examples: int

    model_name: str

    recommended_threshold: float | None
    recommendation_reason: str

    threshold_results: list[ThresholdEvaluationResult]
    pair_results: list[EvaluationPairResult] = Field(default_factory=list)
