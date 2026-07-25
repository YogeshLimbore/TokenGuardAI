from celery import Celery
from celery.schedules import crontab

from app.core.config import get_settings

settings = get_settings()


celery_app = Celery(
    "tokenguard",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "app.workers.tasks.evaluation_tasks",
        "app.workers.tasks.cleanup_tasks",
        "app.workers.tasks.usage_tasks",
    ],
)


celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=(settings.celery_task_time_limit_seconds),
    task_soft_time_limit=(settings.celery_task_soft_time_limit_seconds),
    result_expires=settings.celery_result_expires_seconds,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
)


celery_app.conf.beat_schedule = {
    "cleanup-expired-cache": {
        "task": "tokenguard.cleanup_expired_cache",
        "schedule": (settings.cache_cleanup_interval_hours * 3600),
    },
    "aggregate-project-usage": {
        "task": "tokenguard.aggregate_project_usage",
        "schedule": (settings.usage_aggregation_interval_minutes * 60),
    },
    "cleanup-old-request-logs": {
        "task": "tokenguard.cleanup_old_request_logs",
        "schedule": crontab(
            hour=2,
            minute=0,
        ),
    },
}
