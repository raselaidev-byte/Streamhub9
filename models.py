"""
Job data models. A Job is the single source of truth for every recording.
State transitions are enforced here.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    QUEUED = "queued"
    PROBING = "probing"       # ffprobe running
    RECORDING = "recording"
    PAUSED = "paused"
    STOPPING = "stopping"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobPriority(int, Enum):
    NORMAL = 5
    HIGH = 3      # VIP users
    CRITICAL = 1  # Admin


# Legal transitions map
TRANSITIONS: Dict[JobStatus, List[JobStatus]] = {
    JobStatus.QUEUED:     [JobStatus.PROBING, JobStatus.CANCELLED, JobStatus.FAILED],
    JobStatus.PROBING:    [JobStatus.RECORDING, JobStatus.FAILED, JobStatus.CANCELLED],
    JobStatus.RECORDING:  [JobStatus.PAUSED, JobStatus.STOPPING, JobStatus.COMPLETED, JobStatus.FAILED],
    JobStatus.PAUSED:     [JobStatus.RECORDING, JobStatus.STOPPING, JobStatus.CANCELLED],
    JobStatus.STOPPING:   [JobStatus.COMPLETED, JobStatus.FAILED],
    JobStatus.COMPLETED:  [],
    JobStatus.FAILED:     [JobStatus.QUEUED],  # retry
    JobStatus.CANCELLED:  [],
}


class StreamInfo(BaseModel):
    """Detected stream metadata from ffprobe."""
    url: str
    width: Optional[int] = None
    height: Optional[int] = None
    video_codec: Optional[str] = None
    audio_codec: Optional[str] = None
    bitrate_kbps: Optional[int] = None
    fps: Optional[float] = None
    is_live: bool = True
    duration_seconds: Optional[float] = None  # None = live/unknown


class RecordingOptions(BaseModel):
    """User-specified recording options."""
    duration_seconds: Optional[int] = None    # None = until stopped
    quality: str = "original"                 # original | high | medium | low
    output_format: str = "mp4"
    segment: bool = False                     # segment long recordings
    segment_duration: int = 3600              # seconds per segment
    audio_only: bool = False


class JobProgress(BaseModel):
    """Mutable progress snapshot sent to Telegram."""
    elapsed_seconds: int = 0
    file_size_mb: float = 0.0
    bitrate_kbps: Optional[float] = None
    frames: Optional[int] = None
    speed: Optional[float] = None            # 1.0 = real-time
    output_files: List[str] = Field(default_factory=list)
    retry_count: int = 0
    last_updated: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Job(BaseModel):
    """Immutable-ish job descriptor stored in Redis."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8].upper())
    user_id: int
    chat_id: int
    message_id: Optional[int] = None        # progress message to edit

    # Stream
    stream_url: str
    stream_info: Optional[StreamInfo] = None
    options: RecordingOptions = Field(default_factory=RecordingOptions)

    # State
    status: JobStatus = JobStatus.QUEUED
    priority: JobPriority = JobPriority.NORMAL
    celery_task_id: Optional[str] = None
    ffmpeg_pid: Optional[int] = None

    # Progress
    progress: JobProgress = Field(default_factory=JobProgress)

    # Paths
    output_dir: Optional[str] = None        # resolved at worker start

    # Timestamps
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None

    # Error tracking
    error_message: Optional[str] = None
    retry_count: int = 0

    def transition(self, new_status: JobStatus) -> "Job":
        """Return a new Job with validated status transition."""
        allowed = TRANSITIONS.get(self.status, [])
        if new_status not in allowed:
            raise ValueError(
                f"Invalid transition {self.status} → {new_status}. "
                f"Allowed: {[s.value for s in allowed]}"
            )
        return self.model_copy(update={"status": new_status})

    @property
    def is_active(self) -> bool:
        return self.status in {JobStatus.PROBING, JobStatus.RECORDING, JobStatus.PAUSED}

    @property
    def is_terminal(self) -> bool:
        return self.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}

    @property
    def elapsed_human(self) -> str:
        s = self.progress.elapsed_seconds
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{sec:02d}"

    @property
    def status_emoji(self) -> str:
        return {
            JobStatus.QUEUED:    "⏳",
            JobStatus.PROBING:   "🔍",
            JobStatus.RECORDING: "🔴",
            JobStatus.PAUSED:    "⏸",
            JobStatus.STOPPING:  "🛑",
            JobStatus.COMPLETED: "✅",
            JobStatus.FAILED:    "❌",
            JobStatus.CANCELLED: "🚫",
        }[self.status]
