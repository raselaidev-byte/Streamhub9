"""
Optional FastAPI admin dashboard.
Exposes a REST API for monitoring jobs, system stats, and admin actions.
Protected by a Bearer token (DASHBOARD_SECRET env var).
Start separately: uvicorn api.dashboard:app --port 8080
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config.settings import settings
from core.job_manager import async_job_manager
from core.models import Job, JobStatus
from monitoring.metrics import get_system_stats

app = FastAPI(
    title="StreamBot Dashboard",
    version="1.0.0",
    docs_url="/docs" if settings.DASHBOARD_ENABLED else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_bearer = HTTPBearer()


async def _auth(creds: HTTPAuthorizationCredentials = Depends(_bearer)):
    if creds.credentials != settings.DASHBOARD_SECRET:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )
    return creds.credentials


# ── Routes ────────────────────────────────────────────────────────────────────


@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics", dependencies=[Depends(_auth)])
async def metrics() -> Dict[str, Any]:
    stats = get_system_stats()
    return {
        "cpu_percent": stats.cpu_percent,
        "memory_percent": stats.memory_percent,
        "memory_used_gb": round(stats.memory_used_gb, 2),
        "disk": {
            "used_percent": round(stats.disk.used_percent, 1),
            "free_gb": round(stats.disk.free_gb, 2),
            "bot_used_gb": round(stats.disk.bot_used_gb, 2),
        },
        "ffmpeg_processes": stats.ffmpeg_processes,
    }


@app.get("/jobs", dependencies=[Depends(_auth)])
async def list_all_jobs() -> List[Dict[str, Any]]:
    """Return all active jobs across all users."""
    # Scan Redis for all jobs — admin only endpoint
    import redis.asyncio as aioredis

    r = aioredis.from_url(
        settings.REDIS_URL.replace("/0", f"/{settings.REDIS_JOB_DB}"),
        decode_responses=True,
    )
    keys = await r.keys("job:*")
    jobs = []
    for key in keys:
        raw = await r.get(key)
        if raw:
            try:
                j = Job.model_validate_json(raw)
                jobs.append({
                    "id": j.id,
                    "user_id": j.user_id,
                    "status": j.status.value,
                    "elapsed": j.progress.elapsed_seconds,
                    "size_mb": round(j.progress.file_size_mb, 1),
                    "url": j.stream_url[:60],
                    "created_at": j.created_at.isoformat(),
                })
            except Exception:
                pass
    await r.aclose()
    return sorted(jobs, key=lambda x: x["created_at"], reverse=True)


@app.post("/jobs/{job_id}/stop", dependencies=[Depends(_auth)])
async def force_stop(job_id: str) -> Dict[str, str]:
    from workers.recording_worker import stop_recording

    job = await async_job_manager.get(job_id.upper())
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.is_terminal:
        raise HTTPException(status_code=400, detail="Job already finished")
    stop_recording.apply_async(kwargs={"job_id": job_id.upper()}, queue="priority")
    return {"status": "stop_signal_sent"}


@app.delete("/jobs/{job_id}", dependencies=[Depends(_auth)])
async def delete_job(job_id: str) -> Dict[str, str]:
    job = await async_job_manager.get(job_id.upper())
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.is_terminal:
        raise HTTPException(status_code=400, detail="Stop the job before deleting")
    from core.job_manager import job_manager
    job_manager.delete(job)
    return {"status": "deleted"}
