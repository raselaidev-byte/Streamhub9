"""
Cleanup manager: delete recordings older than FILE_RETENTION_HOURS.
Also enforces MAX_DISK_USAGE_GB by deleting oldest files first.
"""

from __future__ import annotations

import time
from pathlib import Path

from config.settings import settings
from monitoring.logger import get_logger
from monitoring.metrics import get_disk_stats

log = get_logger(__name__)


def cleanup_user_files(storage_root: Path) -> int:
    """
    Walk all user directories and delete files beyond retention window.
    Returns count of deleted files.
    """
    deleted = 0
    cutoff = time.time() - (settings.FILE_RETENTION_HOURS * 3600)

    for user_dir in storage_root.glob("user_*"):
        if not user_dir.is_dir():
            continue
        for fpath in user_dir.rglob("job_*"):
            if not fpath.is_file():
                continue
            try:
                if fpath.stat().st_mtime < cutoff:
                    fpath.unlink()
                    log.info("retention_cleanup", path=str(fpath))
                    deleted += 1
            except OSError as e:
                log.warning("cleanup_error", path=str(fpath), error=str(e))

    # Emergency cleanup: if disk still critical, delete oldest files
    stats = get_disk_stats()
    if stats.bot_used_gb >= settings.MAX_DISK_USAGE_GB:
        deleted += _emergency_cleanup(storage_root)

    return deleted


def _emergency_cleanup(storage_root: Path) -> int:
    """Delete oldest files until under disk limit."""
    all_files = sorted(
        (f for f in storage_root.rglob("job_*") if f.is_file()),
        key=lambda p: p.stat().st_mtime,
    )
    deleted = 0
    for fpath in all_files:
        stats = get_disk_stats()
        if stats.bot_used_gb < settings.MAX_DISK_USAGE_GB * 0.8:
            break
        try:
            size_mb = fpath.stat().st_size / 1e6
            fpath.unlink()
            log.warning("emergency_cleanup", path=str(fpath), size_mb=round(size_mb, 1))
            deleted += 1
        except OSError:
            pass
    return deleted
