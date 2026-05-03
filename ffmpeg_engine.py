"""
FFmpeg recording engine.
- Builds adaptive FFmpeg command lines
- Launches and monitors FFmpeg subprocess
- Parses real-time progress from FFmpeg stderr
- Handles auto-reconnect and crash recovery
- Supports pause/resume via SIGSTOP/SIGCONT
- Supports segmented recordings
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

from config.settings import settings
from core.models import Job, JobProgress, JobStatus, RecordingOptions, StreamInfo
from core.stream_analyzer import quality_to_ffmpeg_args
from monitoring.logger import get_logger

log = get_logger(__name__)

# Regex patterns for FFmpeg stderr parsing
_DURATION_RE = re.compile(r"time=(\d{2}):(\d{2}):(\d{2})\.(\d+)")
_SIZE_RE = re.compile(r"size=\s*(\d+)kB")
_BITRATE_RE = re.compile(r"bitrate=\s*([\d.]+)kbits/s")
_SPEED_RE = re.compile(r"speed=\s*([\d.]+)x")
_FRAME_RE = re.compile(r"frame=\s*(\d+)")


class FFmpegEngine:
    """
    Manages a single FFmpeg recording session.
    One instance per Job.
    """

    def __init__(
        self,
        job: Job,
        output_dir: Path,
        on_progress: Optional[Callable[[JobProgress], None]] = None,
        on_exit: Optional[Callable[[int], None]] = None,
    ) -> None:
        self.job = job
        self.output_dir = output_dir
        self.on_progress = on_progress
        self.on_exit = on_exit
        self._process: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._progress = JobProgress()
        self._start_time: Optional[float] = None
        self._stop_event = threading.Event()

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> int:
        """
        Build command, launch FFmpeg, start stderr monitor thread.
        Returns FFmpeg PID.
        """
        cmd = self._build_command()
        log.info(
            "ffmpeg_start",
            job_id=self.job.id,
            cmd=" ".join(cmd),
        )
        self._start_time = time.monotonic()
        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._thread = threading.Thread(
            target=self._monitor_stderr, daemon=True, name=f"ffmpeg-{self.job.id}"
        )
        self._thread.start()
        return self._process.pid

    def pause(self) -> bool:
        """Send SIGSTOP to FFmpeg process."""
        if not self._process or self._process.poll() is not None:
            return False
        try:
            os.kill(self._process.pid, signal.SIGSTOP)
            log.info("ffmpeg_paused", job_id=self.job.id, pid=self._process.pid)
            return True
        except ProcessLookupError:
            return False

    def resume(self) -> bool:
        """Send SIGCONT to FFmpeg process."""
        if not self._process or self._process.poll() is not None:
            return False
        try:
            os.kill(self._process.pid, signal.SIGCONT)
            log.info("ffmpeg_resumed", job_id=self.job.id, pid=self._process.pid)
            return True
        except ProcessLookupError:
            return False

    def stop(self) -> None:
        """Gracefully stop FFmpeg: send 'q', wait, then SIGTERM, then SIGKILL."""
        self._stop_event.set()
        if not self._process:
            return
        if self._process.poll() is not None:
            return
        try:
            # First: send 'q' to stdin (FFmpeg graceful quit)
            if self._process.stdin:
                self._process.stdin.write("q\n")
                self._process.stdin.flush()
        except OSError:
            pass
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            log.warning("ffmpeg_force_kill", job_id=self.job.id, pid=self._process.pid)
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        """Block until FFmpeg exits. Returns return code."""
        if not self._process:
            return None
        try:
            return self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    @property
    def pid(self) -> Optional[int]:
        return self._process.pid if self._process else None

    @property
    def is_running(self) -> bool:
        return bool(self._process and self._process.poll() is None)

    @property
    def return_code(self) -> Optional[int]:
        return self._process.poll() if self._process else None

    @property
    def output_files(self) -> List[Path]:
        """All files produced so far."""
        return sorted(self.output_dir.glob(f"job_{self.job.id}*"))

    # ── Command builder ───────────────────────────────────────────────────────

    def _build_command(self) -> List[str]:
        opts = self.job.options
        stream_info: Optional[StreamInfo] = self.job.stream_info
        url = self.job.stream_url

        cmd = [settings.FFMPEG_PATH]

        # ── Input flags ────────────────────────────────────────────────────
        cmd += [
            "-loglevel", "info",
            "-stats",
            "-reconnect", "1",
            "-reconnect_at_eof", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", str(settings.FFMPEG_RECONNECT_DELAY),
            "-timeout", "30000000",           # 30 seconds socket timeout
            "-user_agent", "Mozilla/5.0 (compatible; StreamBot/1.0)",
            "-headers", "Accept: */*\r\n",
            "-buffer_size", settings.FFMPEG_BUFFER_SIZE,
            "-thread_queue_size", "4096",
            "-threads", str(settings.FFMPEG_THREAD_COUNT),
        ]

        # ── Duration cap ───────────────────────────────────────────────────
        if opts.duration_seconds:
            cmd += ["-t", str(opts.duration_seconds)]

        cmd += ["-i", url]

        # ── Video / audio codec args ───────────────────────────────────────
        if opts.audio_only:
            cmd += ["-vn", "-c:a", "aac", "-b:a", "192k"]
        else:
            cmd += quality_to_ffmpeg_args(opts.quality, stream_info)

        # ── Audio-video sync ───────────────────────────────────────────────
        cmd += ["-async", "1", "-vsync", "1"]

        # ── Output ─────────────────────────────────────────────────────────
        output_path = self._resolve_output_path(opts)
        cmd += ["-y", str(output_path)]

        return cmd

    def _resolve_output_path(self, opts: RecordingOptions) -> Path:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        if opts.segment:
            # Segmented: produces job_XXXX_001.mp4, job_XXXX_002.mp4 …
            name = f"job_{self.job.id}_{ts}_%03d.{opts.output_format}"
            return self.output_dir / name
        else:
            name = f"job_{self.job.id}_{ts}.{opts.output_format}"
            return self.output_dir / name

    # ── Stderr monitor ────────────────────────────────────────────────────────

    def _monitor_stderr(self) -> None:
        """
        Read FFmpeg stderr line by line.
        Parse progress stats and invoke callback.
        """
        assert self._process and self._process.stderr

        for line in self._process.stderr:
            if self._stop_event.is_set():
                break
            self._parse_progress_line(line.strip())

        rc = self._process.wait()
        log.info("ffmpeg_exit", job_id=self.job.id, returncode=rc)
        if self.on_exit:
            self.on_exit(rc)

    def _parse_progress_line(self, line: str) -> None:
        if not line:
            return

        updated = False

        # Elapsed time
        m = _DURATION_RE.search(line)
        if m:
            h, mn, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
            self._progress = self._progress.model_copy(
                update={"elapsed_seconds": h * 3600 + mn * 60 + s}
            )
            updated = True

        # File size
        m = _SIZE_RE.search(line)
        if m:
            self._progress = self._progress.model_copy(
                update={"file_size_mb": int(m.group(1)) / 1024}
            )
            updated = True

        # Bitrate
        m = _BITRATE_RE.search(line)
        if m:
            self._progress = self._progress.model_copy(
                update={"bitrate_kbps": float(m.group(1))}
            )
            updated = True

        # Speed
        m = _SPEED_RE.search(line)
        if m:
            self._progress = self._progress.model_copy(
                update={"speed": float(m.group(1))}
            )
            updated = True

        # Frame count
        m = _FRAME_RE.search(line)
        if m:
            self._progress = self._progress.model_copy(
                update={"frames": int(m.group(1))}
            )
            updated = True

        if updated and self.on_progress:
            self.on_progress(self._progress)

        # Log errors/warnings from FFmpeg
        lowered = line.lower()
        if "error" in lowered or "invalid" in lowered or "failed" in lowered:
            log.warning("ffmpeg_stderr", job_id=self.job.id, line=line[:300])
