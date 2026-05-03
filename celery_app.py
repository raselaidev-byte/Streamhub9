"""
Celery application factory.
Two queues: 'normal' and 'priority' (VIP users).
"""

from __future__ import annotations

from celery import Celery
from kombu import Exchange, Queue

from config.settings import settings

# ── App ───────────────────────────────────────────────────────────────────────

celery_app = Celery(
    "streambot",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=["workers.recording_worker"],
)

# ── Configuration ─────────────────────────────────────────────────────────────

celery_app.conf.update(
    # Serialization
    task_serializer=settings.CELERY_TASK_SERIALIZER,
    result_serializer="json",
    accept_content=["json"],
    result_expires=settings.CELERY_RESULT_EXPIRES,

    # Routing
    task_default_queue="normal",
    task_queues=(
        Queue(
            "priority",
            Exchange("priority"),
            routing_key="priority",
            queue_arguments={"x-max-priority": 10},
        ),
        Queue(
            "normal",
            Exchange("normal"),
            routing_key="normal",
            queue_arguments={"x-max-priority": 10},
        ),
    ),

    # Worker
    worker_prefetch_multiplier=1,        # one task at a time per worker
    task_acks_late=True,                 # ack after completion (crash safety)
    task_reject_on_worker_lost=True,
    worker_max_tasks_per_child=50,       # recycle workers to prevent memory leaks

    # Concurrency handled externally via --concurrency CLI flag
    # Each FFmpeg process is CPU/IO bound, so 1-4 workers per machine typical

    # Time limits
    task_soft_time_limit=settings.FFMPEG_DEFAULT_TIMEOUT,
    task_time_limit=settings.FFMPEG_DEFAULT_TIMEOUT + 300,

    # Beat schedule (periodic tasks)
    beat_schedule={
        "cleanup-old-files": {
            "task": "workers.recording_worker.cleanup_old_files",
            "schedule": settings.CLEANUP_INTERVAL_SECONDS,
        },
        "watchdog-check": {
            "task": "workers.recording_worker.watchdog_check",
            "schedule": settings.WATCHDOG_CHECK_INTERVAL,
        },
    },

    timezone="UTC",
    enable_utc=True,
)
