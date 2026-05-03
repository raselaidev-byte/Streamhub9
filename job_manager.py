"""
JobManager: Redis-backed persistence for all Job objects.
Uses a dedicated Redis DB so job state is isolated from Celery results.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager, contextmanager
from typing import AsyncIterator, Dict, Iterator, List, Optional

import redis
import redis.asyncio as aioredis

from config.settings import settings
from core.models import Job, JobStatus
from monitoring.logger import get_logger

log = get_logger(__name__)

_JOB_PREFIX = "job:"
_USER_JOBS_PREFIX = "user_jobs:"
_TTL_SECONDS = 86400 * 7  # 7 days


def _job_key(job_id: str) -> str:
    return f"{_JOB_PREFIX}{job_id}"


def _user_key(user_id: int) -> str:
    return f"{_USER_JOBS_PREFIX}{user_id}"


# ── Synchronous client (used by Celery workers) ───────────────────────────────

class JobManager:
    """Synchronous job store for Celery tasks."""

    def __init__(self) -> None:
        self._redis = redis.from_url(
            settings.REDIS_URL.replace("/0", f"/{settings.REDIS_JOB_DB}"),
            decode_responses=True,
        )

    def save(self, job: Job) -> None:
        pipe = self._redis.pipeline()
        pipe.set(_job_key(job.id), job.model_dump_json(), ex=_TTL_SECONDS)
        pipe.sadd(_user_key(job.user_id), job.id)
        pipe.expire(_user_key(job.user_id), _TTL_SECONDS)
        pipe.execute()

    def get(self, job_id: str) -> Optional[Job]:
        raw = self._redis.get(_job_key(job_id))
        if not raw:
            return None
        return Job.model_validate_json(raw)

    def delete(self, job: Job) -> None:
        pipe = self._redis.pipeline()
        pipe.delete(_job_key(job.id))
        pipe.srem(_user_key(job.user_id), job.id)
        pipe.execute()

    def list_user_jobs(self, user_id: int) -> List[Job]:
        job_ids = self._redis.smembers(_user_key(user_id))
        jobs = []
        for jid in job_ids:
            j = self.get(jid)
            if j:
                jobs.append(j)
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)

    def list_active_jobs(self) -> List[Job]:
        """Scan all job keys – use only for admin/watchdog, not hot path."""
        keys = self._redis.keys(f"{_JOB_PREFIX}*")
        jobs = []
        for key in keys:
            raw = self._redis.get(key)
            if raw:
                try:
                    j = Job.model_validate_json(raw)
                    if j.is_active:
                        jobs.append(j)
                except Exception:
                    pass
        return jobs

    def count_user_active(self, user_id: int) -> int:
        return sum(1 for j in self.list_user_jobs(user_id) if j.is_active)

    def count_global_active(self) -> int:
        return len(self.list_active_jobs())

    def update_status(self, job_id: str, status: JobStatus, **extra) -> Optional[Job]:
        job = self.get(job_id)
        if not job:
            return None
        try:
            updated = job.transition(status)
        except ValueError as e:
            log.warning("invalid_transition", job_id=job_id, error=str(e))
            return job
        if extra:
            updated = updated.model_copy(update=extra)
        self.save(updated)
        return updated

    def update_progress(self, job_id: str, **fields) -> Optional[Job]:
        job = self.get(job_id)
        if not job:
            return None
        new_progress = job.progress.model_copy(update=fields)
        updated = job.model_copy(update={"progress": new_progress})
        self.save(updated)
        return updated

    @contextmanager
    def lock(self, job_id: str, timeout: int = 5) -> Iterator[bool]:
        """Distributed lock around job mutation."""
        lock_key = f"lock:job:{job_id}"
        lock = self._redis.lock(lock_key, timeout=timeout)
        acquired = lock.acquire(blocking=True, blocking_timeout=timeout)
        try:
            yield acquired
        finally:
            if acquired:
                lock.release()


# ── Async client (used by Bot / FastAPI) ─────────────────────────────────────

class AsyncJobManager:
    """Async job store for bot handlers and FastAPI."""

    def __init__(self) -> None:
        self._redis = aioredis.from_url(
            settings.REDIS_URL.replace("/0", f"/{settings.REDIS_JOB_DB}"),
            decode_responses=True,
        )

    async def save(self, job: Job) -> None:
        async with self._redis.pipeline() as pipe:
            await pipe.set(_job_key(job.id), job.model_dump_json(), ex=_TTL_SECONDS)
            await pipe.sadd(_user_key(job.user_id), job.id)
            await pipe.expire(_user_key(job.user_id), _TTL_SECONDS)
            await pipe.execute()

    async def get(self, job_id: str) -> Optional[Job]:
        raw = await self._redis.get(_job_key(job_id))
        if not raw:
            return None
        return Job.model_validate_json(raw)

    async def list_user_jobs(self, user_id: int) -> List[Job]:
        job_ids = await self._redis.smembers(_user_key(user_id))
        jobs = []
        for jid in job_ids:
            j = await self.get(jid)
            if j:
                jobs.append(j)
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)

    async def list_active_user_jobs(self, user_id: int) -> List[Job]:
        return [j for j in await self.list_user_jobs(user_id) if j.is_active]

    async def count_user_active(self, user_id: int) -> int:
        return len(await self.list_active_user_jobs(user_id))

    async def count_global_active(self) -> int:
        keys = await self._redis.keys(f"{_JOB_PREFIX}*")
        count = 0
        for key in keys:
            raw = await self._redis.get(key)
            if raw:
                try:
                    j = Job.model_validate_json(raw)
                    if j.is_active:
                        count += 1
                except Exception:
                    pass
        return count

    async def update_status(
        self, job_id: str, status: JobStatus, **extra
    ) -> Optional[Job]:
        job = await self.get(job_id)
        if not job:
            return None
        try:
            updated = job.transition(status)
        except ValueError as e:
            log.warning("invalid_transition", job_id=job_id, error=str(e))
            return job
        if extra:
            updated = updated.model_copy(update=extra)
        await self.save(updated)
        return updated

    async def close(self) -> None:
        await self._redis.aclose()


# Module-level singletons
job_manager = JobManager()
async_job_manager = AsyncJobManager()
