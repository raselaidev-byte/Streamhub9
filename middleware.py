"""
Rate limiting middleware using Redis sliding window.
Blocks users who exceed message limits.
"""

from __future__ import annotations

import time
from typing import Optional

import redis

from config.settings import settings
from monitoring.logger import get_logger

log = get_logger(__name__)

_redis_client = redis.from_url(
    settings.REDIS_URL.replace("/0", f"/{settings.REDIS_CACHE_DB}"),
    decode_responses=True,
)


def check_rate_limit(user_id: int) -> tuple[bool, int]:
    """
    Sliding window rate limiter.
    Returns (allowed: bool, retry_after_seconds: int).
    """
    key = f"rl:{user_id}"
    now = time.time()
    window_start = now - settings.RATE_LIMIT_WINDOW

    pipe = _redis_client.pipeline()
    pipe.zremrangebyscore(key, "-inf", window_start)
    pipe.zadd(key, {str(now): now})
    pipe.zcard(key)
    pipe.expire(key, settings.RATE_LIMIT_WINDOW)
    results = pipe.execute()

    count = results[2]
    if count > settings.RATE_LIMIT_MESSAGES:
        # Find the oldest message to compute retry_after
        oldest = _redis_client.zrange(key, 0, 0, withscores=True)
        if oldest:
            retry_after = int(oldest[0][1] + settings.RATE_LIMIT_WINDOW - now) + 1
        else:
            retry_after = settings.RATE_LIMIT_WINDOW
        log.warning("rate_limit_exceeded", user_id=user_id, count=count)
        return False, retry_after

    return True, 0


def is_blocked(user_id: int) -> bool:
    """Hard block for a user (set by admin)."""
    return user_id in settings.BLOCK_USER_IDS


def can_start_job(user_id: int) -> tuple[bool, str]:
    """Check if user can submit a new recording job."""
    from core.job_manager import job_manager

    active = job_manager.count_user_active(user_id)
    limit = settings.MAX_CONCURRENT_JOBS_PER_USER

    if settings.is_vip(user_id):
        limit = limit * 2  # VIP users get double quota

    if active >= limit:
        return False, (
            f"⚠️ You already have {active}/{limit} active recording(s). "
            f"Stop one before starting a new one."
        )

    global_active = job_manager.count_global_active()
    if global_active >= settings.MAX_TOTAL_CONCURRENT_JOBS:
        return False, (
            "🚦 Server is at max capacity. Please try again in a few minutes."
        )

    return True, ""
