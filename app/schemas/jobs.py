from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field, field_validator

JobStatus = Literal[
    "queued",
    "running",
    "completed",
    "failed",
    "cancelled",
]

# Evaluation datasets must live under this directory. This prevents a
# caller from asking TokenGuard to read arbitrary files from the
# server's filesystem (path traversal / local file disclosure).
ALLOWED_DATASET_DIRECTORY = "evaluation"


class SemanticEvaluationJobRequest(BaseModel):
    dataset_path: str = Field(
        default=("evaluation/semantic_cache_dataset.json"),
        min_length=1,
    )

    @field_validator("dataset_path")
    @classmethod
    def validate_dataset_path(cls, value: str) -> str:
        cleaned_value = value.strip().replace("\\", "/")

        path = PurePosixPath(cleaned_value)

        if path.is_absolute() or ".." in path.parts:
            raise ValueError(
                "dataset_path must be a relative path without '..' segments."
            )

        if path.parts[:1] != (ALLOWED_DATASET_DIRECTORY,):
            raise ValueError(
                "dataset_path must be located under the "
                f"'{ALLOWED_DATASET_DIRECTORY}/' directory."
            )

        if path.suffix != ".json":
            raise ValueError("dataset_path must point to a .json file.")

        return cleaned_value


class BackgroundJobResponse(BaseModel):
    job_id: str
    project_id: int
    job_type: str
    status: JobStatus
    progress_percent: float
    result: dict[str, object] | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
