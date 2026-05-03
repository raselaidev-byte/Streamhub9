"""
Central configuration management using Pydantic Settings.
All environment variables are validated and typed here.
"""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from typing import List, Optional

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class StorageBackend(str, Enum):
    LOCAL = "local"
    GDRIVE = "gdrive"
    TELEGRAM = "telegram"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Telegram ──────────────────────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str
    TELEGRAM_ADMIN_IDS: List[int] = []
    TELEGRAM_UPLOAD_CHANNEL_ID: Optional[int] = None
    TELEGRAM_MAX_FILE_SIZE_MB: int = 2000  # 2 GB Telegram Bot API limit

    # ── Redis ─────────────────────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_JOB_DB: int = 1          # separate DB for job state
    REDIS_CACHE_DB: int = 2        # separate DB for cache

    # ── Celery ────────────────────────────────────────────────────────────────
    CELERY_BROKER_URL: str = "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/0"
    CELERY_TASK_SERIALIZER: str = "json"
    CELERY_RESULT_EXPIRES: int = 86400  # 24 hours
    CELERY_MAX_RETRIES: int = 3
    CELERY_RETRY_DELAY: int = 10        # seconds

    # ── FFmpeg ────────────────────────────────────────────────────────────────
    FFMPEG_PATH: str = "ffmpeg"
    FFPROBE_PATH: str = "ffprobe"
    FFMPEG_THREAD_COUNT: int = 2
    FFMPEG_RECONNECT_ATTEMPTS: int = 10
    FFMPEG_RECONNECT_DELAY: int = 5     # seconds
    FFMPEG_BUFFER_SIZE: str = "32M"
    FFMPEG_SEGMENT_DURATION: int = 3600  # 1 hour per segment
    FFMPEG_DEFAULT_TIMEOUT: int = 7200   # 2 hours max recording
    FFMPEG_PROBE_TIMEOUT: int = 15       # stream probe timeout

    # ── Storage ───────────────────────────────────────────────────────────────
    STORAGE_BACKEND: StorageBackend = StorageBackend.LOCAL
    LOCAL_STORAGE_PATH: Path = Path("/data")
    MAX_DISK_USAGE_GB: float = 50.0
    DISK_USAGE_WARN_PERCENT: float = 80.0
    FILE_RETENTION_HOURS: int = 72       # auto-delete after 72h
    CLEANUP_INTERVAL_SECONDS: int = 3600 # run cleanup every hour

    # ── Google Drive ──────────────────────────────────────────────────────────
    GDRIVE_CREDENTIALS_FILE: Optional[Path] = None
    GDRIVE_FOLDER_ID: Optional[str] = None

    # ── Rate Limiting ─────────────────────────────────────────────────────────
    RATE_LIMIT_MESSAGES: int = 20        # messages per window
    RATE_LIMIT_WINDOW: int = 60          # seconds
    MAX_CONCURRENT_JOBS_PER_USER: int = 3
    MAX_TOTAL_CONCURRENT_JOBS: int = 20
    VIP_USER_IDS: List[int] = []         # priority queue users

    # ── Progress Updates ──────────────────────────────────────────────────────
    PROGRESS_UPDATE_INTERVAL: int = 30   # seconds between Telegram updates
    PROGRESS_EDIT_TIMEOUT: int = 10      # Telegram edit timeout

    # ── Monitoring ────────────────────────────────────────────────────────────
    LOG_LEVEL: LogLevel = LogLevel.INFO
    LOG_FILE: Optional[Path] = Path("/var/log/streambot/bot.log")
    LOG_MAX_BYTES: int = 10_485_760      # 10 MB
    LOG_BACKUP_COUNT: int = 5
    METRICS_ENABLED: bool = True

    # ── FastAPI Dashboard ─────────────────────────────────────────────────────
    DASHBOARD_ENABLED: bool = False
    DASHBOARD_HOST: str = "0.0.0.0"
    DASHBOARD_PORT: int = 8080
    DASHBOARD_SECRET: str = "change-me-in-production"

    # ── Watchdog ──────────────────────────────────────────────────────────────
    WATCHDOG_ENABLED: bool = True
    WATCHDOG_CHECK_INTERVAL: int = 30    # seconds
    WATCHDOG_MAX_WORKER_RESTARTS: int = 5

    # ── Security ──────────────────────────────────────────────────────────────
    ALLOWED_USER_IDS: List[int] = []     # empty = all users allowed
    BLOCK_USER_IDS: List[int] = []

    @field_validator("LOCAL_STORAGE_PATH", mode="before")
    @classmethod
    def ensure_storage_path(cls, v: str | Path) -> Path:
        p = Path(v)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @field_validator("LOG_FILE", mode="before")
    @classmethod
    def ensure_log_dir(cls, v: Optional[str | Path]) -> Optional[Path]:
        if v is None:
            return None
        p = Path(v)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @model_validator(mode="after")
    def validate_gdrive(self) -> "Settings":
        if self.STORAGE_BACKEND == StorageBackend.GDRIVE:
            if not self.GDRIVE_CREDENTIALS_FILE or not self.GDRIVE_FOLDER_ID:
                raise ValueError(
                    "GDRIVE_CREDENTIALS_FILE and GDRIVE_FOLDER_ID are required "
                    "when STORAGE_BACKEND=gdrive"
                )
        return self

    def user_storage_path(self, user_id: int) -> Path:
        """Return isolated storage directory for a user."""
        p = self.LOCAL_STORAGE_PATH / f"user_{user_id}"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.TELEGRAM_ADMIN_IDS

    def is_vip(self, user_id: int) -> bool:
        return user_id in self.VIP_USER_IDS or self.is_admin(user_id)

    def is_allowed(self, user_id: int) -> bool:
        if user_id in self.BLOCK_USER_IDS:
            return False
        if not self.ALLOWED_USER_IDS:
            return True
        return user_id in self.ALLOWED_USER_IDS


# Singleton — import this everywhere
settings = Settings()
