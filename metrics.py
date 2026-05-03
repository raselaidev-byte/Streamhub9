"""
System metrics: disk space, memory, active process counts.
Used by watchdog and dashboard.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path

import psutil

from config.settings import settings
from monitoring.logger import get_logger

log = get_logger(__name__)


@dataclass
class DiskStats:
    total_gb: float
    used_gb: float
    free_gb: float
    used_percent: float
    bot_used_gb: float  # bytes used by our storage dir


@dataclass
class SystemStats:
    disk: DiskStats
    cpu_percent: float
    memory_percent: float
    memory_used_gb: float
    ffmpeg_processes: int


def get_disk_stats() -> DiskStats:
    usage = shutil.disk_usage(str(settings.LOCAL_STORAGE_PATH))
    bot_used = _dir_size(settings.LOCAL_STORAGE_PATH)

    return DiskStats(
        total_gb=usage.total / 1e9,
        used_gb=usage.used / 1e9,
        free_gb=usage.free / 1e9,
        used_percent=usage.used / usage.total * 100,
        bot_used_gb=bot_used / 1e9,
    )


def get_system_stats() -> SystemStats:
    disk = get_disk_stats()
    mem = psutil.virtual_memory()
    ffmpeg_count = sum(
        1 for p in psutil.process_iter(["name"])
        if p.info["name"] == "ffmpeg"  # type: ignore[index]
    )
    return SystemStats(
        disk=disk,
        cpu_percent=psutil.cpu_percent(interval=0.5),
        memory_percent=mem.percent,
        memory_used_gb=mem.used / 1e9,
        ffmpeg_processes=ffmpeg_count,
    )


def is_disk_critical() -> bool:
    stats = get_disk_stats()
    over_limit = stats.bot_used_gb >= settings.MAX_DISK_USAGE_GB
    low_free = stats.used_percent >= 95.0
    return over_limit or low_free


def is_disk_warning() -> bool:
    stats = get_disk_stats()
    return stats.used_percent >= settings.DISK_USAGE_WARN_PERCENT


def _dir_size(path: Path) -> int:
    """Recursively sum file sizes under path."""
    total = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                try:
                    total += entry.stat().st_size
                except OSError:
                    pass
    except PermissionError:
        pass
    return total


async def monitor_disk_loop() -> None:
    """Background coroutine: log disk warnings periodically."""
    while True:
        try:
            stats = get_disk_stats()
            if is_disk_critical():
                log.error(
                    "disk_critical",
                    used_percent=round(stats.used_percent, 1),
                    bot_used_gb=round(stats.bot_used_gb, 2),
                    free_gb=round(stats.free_gb, 2),
                )
            elif is_disk_warning():
                log.warning(
                    "disk_warning",
                    used_percent=round(stats.used_percent, 1),
                    free_gb=round(stats.free_gb, 2),
                )
        except Exception:
            log.exception("disk_monitor_error")
        await asyncio.sleep(300)  # every 5 minutes
