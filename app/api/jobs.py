import json
from uuid import uuid4

from celery.result import AsyncResult
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    status,
)
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database.connection import get_database_session
from app.database.models import BackgroundJob, Project
from app.dependencies.authentication import get_current_project
from app.schemas.jobs import (
    BackgroundJobResponse,
    SemanticEvaluationJobRequest,
)
from app.workers.celery_app import celery_app
from app.workers.tasks.evaluation_tasks import (
    run_semantic_evaluation,
)

router = APIRouter(
    prefix="/v1/jobs",
    tags=["Background Jobs"],
)


def job_to_response(
    job: BackgroundJob,
) -> BackgroundJobResponse:
    result = None

    if job.result_json:
        result = json.loads(job.result_json)

    return BackgroundJobResponse(
        job_id=job.job_id,
        project_id=job.project_id,
        job_type=job.job_type,
        status=job.status,
        progress_percent=job.progress_percent,
        result=result,
        error_message=job.error_message,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
    )


def _get_job_or_404(
    database: Session,
    *,
    job_id: str,
    project_id: int,
) -> BackgroundJob:
    statement = select(BackgroundJob).where(
        BackgroundJob.job_id == job_id,
        BackgroundJob.project_id == project_id,
    )

    job = database.scalar(statement)

    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job was not found.",
        )

    return job


@router.post(
    "/evaluations/semantic-cache",
    response_model=BackgroundJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_semantic_evaluation_job(
    request: SemanticEvaluationJobRequest,
    project: Project = Depends(get_current_project),
    database: Session = Depends(get_database_session),
) -> BackgroundJobResponse:
    # Serialize admission for this project so concurrent requests cannot all
    # pass the active-job quota before inserting their rows.
    database.scalar(select(Project).where(Project.id == project.id).with_for_update())
    from app.core.config import get_settings

    active_jobs = database.scalar(
        select(func.count(BackgroundJob.id)).where(
            BackgroundJob.project_id == project.id,
            BackgroundJob.status.in_({"queued", "running"}),
        )
    )
    if (active_jobs or 0) >= get_settings().maximum_active_jobs_per_project:
        database.rollback()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="The project has reached its active background-job limit.",
        )

    job_id = str(uuid4())

    job = BackgroundJob(
        job_id=job_id,
        project_id=project.id,
        job_type="semantic_cache_evaluation",
        status="queued",
        progress_percent=0.0,
        input_json=request.model_dump_json(),
    )

    database.add(job)
    database.commit()
    database.refresh(job)

    try:
        run_semantic_evaluation.apply_async(
            task_id=job_id,
            kwargs={
                "database_job_id": job.id,
                "project_id": project.id,
                "dataset_path": request.dataset_path,
            },
        )
    except Exception as error:
        job.status = "failed"
        job.error_message = "The job could not be published to the worker queue."
        database.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The background-job service is unavailable.",
        ) from error

    return job_to_response(job)


@router.get(
    "/{job_id}",
    response_model=BackgroundJobResponse,
)
async def get_job(
    job_id: str,
    project: Project = Depends(get_current_project),
    database: Session = Depends(get_database_session),
) -> BackgroundJobResponse:
    job = _get_job_or_404(
        database,
        job_id=job_id,
        project_id=project.id,
    )

    if job.status in {"queued", "running"}:
        celery_result = AsyncResult(
            job_id,
            app=celery_app,
        )

        if celery_result.state == "PROGRESS" and isinstance(
            celery_result.info,
            dict,
        ):
            job.status = "running"
            job.progress_percent = float(
                celery_result.info.get(
                    "progress_percent",
                    job.progress_percent,
                )
            )

            database.commit()
            database.refresh(job)

    return job_to_response(job)


@router.delete(
    "/{job_id}",
    response_model=BackgroundJobResponse,
)
async def cancel_job(
    job_id: str,
    project: Project = Depends(get_current_project),
    database: Session = Depends(get_database_session),
) -> BackgroundJobResponse:
    job = _get_job_or_404(
        database,
        job_id=job_id,
        project_id=project.id,
    )

    if job.status in {"completed", "failed", "cancelled"}:
        return job_to_response(job)

    celery_app.control.revoke(
        job_id,
        terminate=False,
    )

    job.status = "cancelled"
    database.commit()
    database.refresh(job)

    return job_to_response(job)
