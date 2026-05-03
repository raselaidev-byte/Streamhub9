"""
Stream Analyzer: uses ffprobe to inspect M3U8 URLs before recording.
Returns StreamInfo used to auto-tune FFmpeg parameters.
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional

from config.settings import settings
from core.models import StreamInfo
from monitoring.logger import get_logger

log = get_logger(__name__)

_FFPROBE_CMD_TEMPLATE = [
    "{ffprobe}",
    "-v", "quiet",
    "-print_format", "json",
    "-show_streams",
    "-show_format",
    "-timeout", "{timeout}000000",   # microseconds
    "-user_agent", "Mozilla/5.0 (compatible; StreamBot/1.0)",
    "{url}",
]


async def probe_stream(url: str) -> Optional[StreamInfo]:
    """
    Run ffprobe on an M3U8 URL and return StreamInfo.
    Returns None if the stream is dead / unreachable.
    """
    cmd = [
        part.format(
            ffprobe=settings.FFPROBE_PATH,
            timeout=settings.FFMPEG_PROBE_TIMEOUT,
            url=url,
        )
        for part in _FFPROBE_CMD_TEMPLATE
    ]

    log.info("probing_stream", url=url)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=settings.FFMPEG_PROBE_TIMEOUT + 5
        )
    except asyncio.TimeoutError:
        log.warning("probe_timeout", url=url)
        return None
    except OSError as e:
        log.error("probe_os_error", url=url, error=str(e))
        return None

    if proc.returncode != 0:
        log.warning(
            "probe_failed",
            url=url,
            returncode=proc.returncode,
            stderr=stderr.decode(errors="replace")[:500],
        )
        return None

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        log.warning("probe_invalid_json", url=url)
        return None

    return _parse_probe(url, data)


def _parse_probe(url: str, data: dict) -> StreamInfo:
    streams = data.get("streams", [])
    fmt = data.get("format", {})

    info = StreamInfo(url=url)

    # Detect live stream
    tags = fmt.get("tags", {})
    info.is_live = "variant_bitrate" in tags or _has_hls_segment(streams)

    raw_duration = fmt.get("duration")
    if raw_duration and float(raw_duration) > 0:
        info.duration_seconds = float(raw_duration)

    # Total bitrate
    raw_bitrate = fmt.get("bit_rate")
    if raw_bitrate:
        info.bitrate_kbps = int(raw_bitrate) // 1000

    for stream in streams:
        codec_type = stream.get("codec_type")
        if codec_type == "video":
            info.video_codec = stream.get("codec_name")
            info.width = stream.get("width")
            info.height = stream.get("height")
            # Parse fps from avg_frame_rate
            afr = stream.get("avg_frame_rate", "0/0")
            try:
                num, den = map(int, afr.split("/"))
                if den:
                    info.fps = round(num / den, 2)
            except (ValueError, ZeroDivisionError):
                pass
        elif codec_type == "audio":
            info.audio_codec = stream.get("codec_name")

    log.info(
        "probe_success",
        url=url,
        resolution=f"{info.width}x{info.height}" if info.width else "unknown",
        bitrate_kbps=info.bitrate_kbps,
        is_live=info.is_live,
    )
    return info


def _has_hls_segment(streams: list) -> bool:
    for s in streams:
        tags = s.get("tags", {})
        if "variant_bitrate" in tags:
            return True
    return False


def quality_to_ffmpeg_args(quality: str, stream_info: Optional[StreamInfo]) -> list[str]:
    """
    Translate user quality preference into FFmpeg video filter / codec args.
    """
    if quality == "original" or stream_info is None:
        # Copy streams, no re-encode = fast and lossless
        return ["-c:v", "copy", "-c:a", "copy"]

    # Re-encode targets
    targets = {
        "high":   {"scale": None,       "crf": "18", "preset": "fast"},
        "medium": {"scale": "1280:720", "crf": "23", "preset": "fast"},
        "low":    {"scale": "854:480",  "crf": "28", "preset": "faster"},
    }
    cfg = targets.get(quality, targets["medium"])
    args = ["-c:v", "libx264", "-crf", cfg["crf"], "-preset", cfg["preset"]]
    if cfg["scale"]:
        args += ["-vf", f"scale={cfg['scale']}"]
    args += ["-c:a", "aac", "-b:a", "128k"]
    return args
