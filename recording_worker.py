"""
Celery recording worker.
Each task manages one FFmpeg process end-to-end:
  1. Probe the stream
  2. Set up output directory
  3. Start FFmpeg
  4. Push progress updates to Redis (bot reads them)
  5. Handle retries on failure
  6. Finalize (move files, trigger upload)
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from celery import Task
from celery.exceptions import SoftTimeLimitExceeded
from celery.utils.log import get_task_logger

from config.settings import settings
from core.ffmpeg_engine import FFmpegEngine
from core.job_manager import job_manager
from core.models import Job, JobProgress, JobStatus, StreamInfo
from core.stream_analyzer import probe_stream
from storage.cleanup_manager import cleanup_user_files
from storage.local_storage import LocalStorage
from workers.celery_app import celery_app

logger = get_task_logger(__name__)

# How often to write progress to Redis (seconds) — bot reads this
_PROGRESS_FLUSH_INTERVAL = 5


class RecordingTask(Task):
    """Custom base task with shared state."""
    abstract = True
    _engine: Optional[FFmpegEngine] = None

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        job_id = kwargs.get("job_id") or (args[0] if args else None)
        if job_id:
            job_manager.update_status(
                job_id,
                JobStatus.FAILED,
                error_message=str(exc),
                ended_at=datetime.now(timezone.utc),
            )
        logger.error(f"Task failed: {exc}", exc_info=True)

    def on_retry(self, exc, task_id, args, kwargs, einfo):
        job_id = kwargs.get("job_id") or (args[0] if args else None)
        if job_id:
            job = job_manager.get(job_id)
            if job:
                job_manager.update_status(
                    job_id,
                    JobStatus.QUEUED,
                    retry_count=job.retry_count + 1,
                )
        logger.warning(f"Task retrying: {exc}")


@celery_app.task(
    bind=True,
    base=RecordingTask,
    name="workers.recording_worker.record_stream",
    max_retries=settings.CELERY_MAX_RETRIES,
    default_retry_delay=settings.CELERY_RETRY_DELAY,
    queue="normal",
)
def record_stream(self: RecordingTask, job_id: str) -> dict[str, Any]:
    """
    Main recording task.
    Called by bot when user issues /record.
    """
    job = job_manager.get(job_id)
    if not job:
        raise ValueError(f"Job {job_id} not found in Redis")

    if job.is_terminal:
        return {"status": "already_terminal", "job_id": job_id}

    # ── 1. Probe stream ───────────────────────────────────────────────────────
    job = job_manager.update_status(job_id, JobStatus.PROBING) or job
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        stream_info: Optional[StreamInfo] = loop.run_until_complete(
            probe_stream(job.stream_url)
        )
    finally:
        loop.close()

    if stream_info is None:
        logger.warning(f"Stream probe failed for job {job_id}, url={job.stream_url}")
        raise self.retry(
            exc=RuntimeError(f"Cannot reach stream: {job.stream_url}"),
            countdown=settings.CELERY_RETRY_DELAY,
        )

    # Save stream info
    job = job_manager.get(job_id) or job
    job = job.model_copy(update={"stream_info": stream_info})
    job_manager.save(job)

    # ── 2. Setup output directory ─────────────────────────────────────────────
    output_dir = settings.user_storage_path(job.user_id)
    job = job.model_copy(update={"output_dir": str(output_dir)})
    job_manager.save(job)

    # ── 3. Start FFmpeg ───────────────────────────────────────────────────────
    last_progress_flush = time.monotonic()
    last_progress = JobProgress()

    def on_progress(progress: JobProgress) -> None:
        nonlocal last_progress, last_progress_flush
        last_progress = progress
        now = time.monotonic()
        if now - last_progress_flush >= _PROGRESS_FLUSH_INTERVAL:
            _flush_progress(job_id, progress)
            last_progress_flush = now

    def on_exit(rc: int) -> None:
        logger.info(f"FFmpeg exited: job={job_id} rc={rc}")

    engine = FFmpegEngine(
        job=job,
        output_dir=output_dir,
        on_progress=on_progress,
        on_exit=on_exit,
    )
    self._engine = engine

    pid = engine.start()
    job_manager.update_status(
        job_id,
        JobStatus.RECORDING,
        ffmpeg_pid=pid,
        started_at=datetime.now(timezone.utc),
    )

    # ── 4. Wait loop ──────────────────────────────────────────────────────────
    try:
        while engine.is_running:
            # Check for stop/pause signals from Redis
            current = job_manager.get(job_id)
            if current:
                if current.status == JobStatus.STOPPING:
                    engine.stop()
                    break
                elif current.status == JobStatus.PAUSED and engine.is_running:
                    engine.pause()
                elif current.status == JobStatus.RECORDING and not engine.is_running:
                    # Should not happen, but guard
                    break

            # Flush final progress periodically
            _flush_progress(job_id, last_progress)
            time.sleep(2)

    except SoftTimeLimitExceeded:
        logger.warning(f"Soft time limit reached for job {job_id}, stopping gracefully")
        engine.stop()

    except Exception as exc:
        engine.stop()
        raise self.retry(exc=exc, countdown=settings.CELERY_RETRY_DELAY)

    # ── 5. Finalise ───────────────────────────────────────────────────────────
    rc = engine.return_code or 0
    output_files = [str(f) for f in engine.output_files]

    if rc == 0 or output_files:
        job_manager.update_status(
            job_id,
            JobStatus.COMPLETED,
            ended_at=datetime.now(timezone.utc),
        )
        _flush_progress(
            job_id,
            last_progress.model_copy(update={"output_files": output_files}),
        )
        logger.info(
            f"Recording complete: job={job_id} files={output_files} rc={rc}"
        )
        return {"status": "completed", "job_id": job_id, "files": output_files}
    else:
        raise self.retry(
            exc=RuntimeError(f"FFmpeg exited with code {rc}"),
            countdown=settings.CELERY_RETRY_DELAY,
        )


@celery_app.task(
    name="workers.recording_worker.stop_recording",
    queue="priority",
)
def stop_recording(job_id: str) -> dict:
    """Signal a running recording to stop gracefully."""
    job = job_manager.get(job_id)
    if not job:
        return {"error": "job_not_found"}
    if not job.is_active:
        return {"status": "not_active"}
    job_manager.update_status(job_id, JobStatus.STOPPING)
    return {"status": "stop_signal_sent", "job_id": job_id}


@celery_app.task(
    name="workers.recording_worker.pause_recording",
    queue="priority",
)
def pause_recording(job_id: str) -> dict:
    """Signal a recording to pause via SIGSTOP."""
    job = job_manager.get(job_id)
    if not job or job.status != JobStatus.RECORDING:
        return {"error": "not_recording"}
    job_manager.update_status(job_id, JobStatus.PAUSED)
    return {"status": "paused", "job_id": job_id}


@celery_app.task(
    name="workers.recording_worker.resume_recording",
    queue="priority",
)
def resume_recording(job_id: str) -> dict:
    """Signal a paused recording to resume via SIGCONT."""
    job = job_manager.get(job_id)
    if not job or job.status != JobStatus.PAUSED:
        return {"error": "not_paused"}
    job_manager.update_status(job_id, JobStatus.RECORDING)
    return {"status": "resumed", "job_id": job_id}


@celery_app.task(
    name="workers.recording_worker.cleanup_old_files",
    queue="normal",
)
def cleanup_old_files() -> dict:
    """Periodic task: remove recordings older than retention policy."""
    deleted = cleanup_user_files(settings.LOCAL_STORAGE_PATH)
    logger.info(f"Cleanup: deleted {deleted} old files")
    return {"deleted": deleted}


@celery_app.task(
    name="workers.recording_worker.watchdog_check",
    queue="normal",
)
def watchdog_check() -> dict:
    """
    Periodic watchdog: find jobs stuck in RECORDING without an active FFmpeg PID.
    Mark them FAILED so they can be retried.
    """
    import psutil
    stuck = []
    for job in job_manager.list_active_jobs():
        if job.status == JobStatus.RECORDING and job.ffmpeg_pid:
            try:
                proc = psutil.Process(job.ffmpeg_pid)
                if proc.name() != "ffmpeg":
                    raise psutil.NoSuchProcess(job.ffmpeg_pid)
            except psutil.NoSuchProcess:
                logger.warning(f"Watchdog: job {job.id} has dead PID {job.ffmpeg_pid}")
                job_manager.update_status(
                    job.id,
                    JobStatus.FAILED,
                    error_message="FFmpeg process died unexpectedly",
                )
                stuck.append(job.id)
    return {"stuck_jobs_found": len(stuck), "job_ids": stuck}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _flush_progress(job_id: str, progress: JobProgress) -> None:
    """Write progress snapshot to Redis."""
    job_manager.update_progress(
        job_id,
        elapsed_seconds=progress.elapsed_seconds,
        file_size_mb=progress.file_size_mb,
        bitrate_kbps=progress.bitrate_kbps,
        frames=progress.frames,
        speed=progress.speed,
        output_files=progress.output_files,
    )
