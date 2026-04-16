from celery import Celery

from src.config import settings

app = Celery(
    "orange_clipboard",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "src.worker.tasks.email",
        "src.worker.tasks.blob_cleanup",
        "src.worker.tasks.notifications",
    ],
)

app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    beat_schedule={
        "blob-cleanup-daily": {
            "task": "src.worker.tasks.blob_cleanup.cleanup_orphan_blobs",
            "schedule": 86400.0,  # every 24h
        }
    },
)
