import json
from datetime import UTC, datetime

from celery import states
from celery.exceptions import Ignore

from app.database.connection import SessionLocal
from app.database.models import BackgroundJob
from app.services.semantic_evaluation_service import (
    EvaluationDatasetError,
    SemanticEvaluationService,
)
from app.workers.celery_app import celery_app


@celery_app.task(
    bind=True,
    name="tokenguard.run_semantic_evaluation",
)
def run_semantic_evaluation(
    self,
    *,
    database_job_id: int,
    project_id: int,
    dataset_path: str,
) -> dict[str, object]:
    database = SessionLocal()

    try:
        job = database.get(
            BackgroundJob,
            database_job_id,
        )

        if job is None:
            raise RuntimeError("Background-job record was not found.")

        if job.status == "cancelled":
            # The caller cancelled the job before a worker
            # picked it up. Do not overwrite that decision.
            return {"status": "cancelled"}

        job.status = "running"
        job.started_at = datetime.now(UTC)
        job.progress_percent = 5.0
        database.commit()

        self.update_state(
            state="PROGRESS",
            meta={
                "progress_percent": 5,
                "message": "Loading evaluation dataset.",
            },
        )

        # Bound outside the closure because type narrowing from the
        # `job is None` check above does not survive into nested functions.
        job_record = job

        def progress_callback(value: float) -> None:
            self.update_state(
                state="PROGRESS",
                meta={"progress_percent": value},
            )

            # Persist progress so that HTTP polling clients see it
            # even between Celery result-backend refreshes.
            job_record.progress_percent = value
            database.commit()

        service = SemanticEvaluationService(dataset_path=dataset_path)

        result = service.run_evaluation(progress_callback=progress_callback)

        result_payload = result.model_dump()

        job.status = "completed"
        job.progress_percent = 100.0
        job.result_json = json.dumps(
            result_payload,
            ensure_ascii=False,
        )
        job.completed_at = datetime.now(UTC)
        database.commit()

        return result_payload

    except EvaluationDatasetError as error:
        database.rollback()

        job = database.get(
            BackgroundJob,
            database_job_id,
        )

        if job is not None:
            job.status = "failed"
            job.error_message = str(error)
            job.completed_at = datetime.now(UTC)
            database.commit()

        self.update_state(
            state=states.FAILURE,
            meta={
                "error_type": type(error).__name__,
                "message": str(error),
            },
        )

        raise Ignore() from error

    except Exception as error:
        database.rollback()

        job = database.get(
            BackgroundJob,
            database_job_id,
        )

        if job is not None:
            job.status = "failed"
            job.error_message = str(error)
            job.completed_at = datetime.now(UTC)
            database.commit()

        self.update_state(
            state=states.FAILURE,
            meta={
                "error_type": type(error).__name__,
                "message": str(error),
            },
        )

        raise Ignore() from error

    finally:
        database.close()
