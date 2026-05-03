"""
Command Parser: converts natural language to structured RecordingOptions.

Examples:
  "record this for 2 hours high quality"
  "start recording https://... for 30 minutes"
  "record https://... low quality audio only"
  "/record https://... 1h medium"
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple
from urllib.parse import urlparse

from core.models import RecordingOptions


# ── URL detection ──────────────────────────────────────────────────────────────

_URL_RE = re.compile(
    r"https?://[^\s]+"
)

_M3U8_EXTS = {".m3u8", ".m3u"}


def extract_url(text: str) -> Optional[str]:
    """Extract first HTTP URL from message. Returns None if not found."""
    m = _URL_RE.search(text)
    if not m:
        return None
    url = m.group(0).rstrip(".,;!?)>")
    return url


def is_m3u8_url(url: str) -> bool:
    """Heuristic: does URL look like an M3U8 stream?"""
    parsed = urlparse(url)
    path = parsed.path.lower()
    if any(path.endswith(ext) for ext in _M3U8_EXTS):
        return True
    # Some streams use query params
    if "m3u8" in url.lower() or "playlist" in url.lower() or "stream" in url.lower():
        return True
    # Accept any http(s) URL — let ffprobe decide
    return parsed.scheme in ("http", "https")


# ── Duration parsing ────────────────────────────────────────────────────────────

_DURATION_PATTERNS = [
    # "2 hours", "2h", "2hr", "2hrs"
    (re.compile(r"(\d+)\s*h(?:ours?|rs?)?(?:\s+(\d+)\s*m(?:in(?:utes?)?)?)?", re.I), "hm"),
    # "30 minutes", "30m", "30min"
    (re.compile(r"(\d+)\s*m(?:in(?:utes?)?)?", re.I), "m"),
    # "90 seconds", "90s", "90sec"
    (re.compile(r"(\d+)\s*s(?:ec(?:onds?)?)?", re.I), "s"),
    # "1:30" (hours:minutes)
    (re.compile(r"(\d+):(\d{2})"), "colon"),
]


def parse_duration(text: str) -> Optional[int]:
    """Return duration in seconds or None."""
    for pattern, kind in _DURATION_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        if kind == "hm":
            h = int(m.group(1))
            mn = int(m.group(2)) if m.group(2) else 0
            return h * 3600 + mn * 60
        elif kind == "m":
            return int(m.group(1)) * 60
        elif kind == "s":
            return int(m.group(1))
        elif kind == "colon":
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60
    return None


# ── Quality parsing ─────────────────────────────────────────────────────────────

_QUALITY_MAP = {
    "high": ["high", "hq", "best", "1080", "720"],
    "medium": ["medium", "med", "mq", "480"],
    "low": ["low", "lq", "small", "360", "240"],
    "original": ["original", "orig", "copy", "lossless", "source"],
}


def parse_quality(text: str) -> str:
    lower = text.lower()
    for quality, keywords in _QUALITY_MAP.items():
        if any(kw in lower for kw in keywords):
            return quality
    return "original"


# ── Format parsing ──────────────────────────────────────────────────────────────

_FORMAT_RE = re.compile(r"\b(mp4|mkv|ts|avi|mov)\b", re.I)


def parse_format(text: str) -> str:
    m = _FORMAT_RE.search(text)
    return m.group(1).lower() if m else "mp4"


# ── Intent detection ────────────────────────────────────────────────────────────

_RECORD_KEYWORDS = {
    "record", "start", "capture", "stream", "download", "grab", "save", "get"
}
_STOP_KEYWORDS = {"stop", "cancel", "abort", "end", "quit", "kill"}
_PAUSE_KEYWORDS = {"pause", "hold", "freeze"}
_RESUME_KEYWORDS = {"resume", "continue", "unpause", "go"}
_STATUS_KEYWORDS = {"status", "list", "show", "jobs", "progress", "what"}


@dataclass
class ParsedCommand:
    intent: str               # record|stop|pause|resume|status|unknown
    url: Optional[str]
    job_id: Optional[str]
    options: RecordingOptions


def parse_command(text: str) -> ParsedCommand:
    """
    Parse any text (command or natural language) into a ParsedCommand.
    """
    lower = text.lower()
    words = set(lower.split())

    # ── Detect intent ──────────────────────────────────────────────────────
    if words & _STOP_KEYWORDS:
        intent = "stop"
    elif words & _PAUSE_KEYWORDS:
        intent = "pause"
    elif words & _RESUME_KEYWORDS:
        intent = "resume"
    elif words & _STATUS_KEYWORDS and not words & _RECORD_KEYWORDS:
        intent = "status"
    elif words & _RECORD_KEYWORDS:
        intent = "record"
    else:
        intent = "unknown"

    # ── Extract URL ────────────────────────────────────────────────────────
    url = extract_url(text)

    # ── Extract job ID (8-char hex, e.g. "A3F2B1C9") ──────────────────────
    job_id_match = re.search(r"\b([A-F0-9]{8})\b", text.upper())
    job_id = job_id_match.group(1) if job_id_match else None

    # Also look for "#ABCDEF12" style
    if not job_id:
        job_id_match = re.search(r"#([A-F0-9]{8})", text.upper())
        job_id = job_id_match.group(1) if job_id_match else None

    # ── Build options ──────────────────────────────────────────────────────
    duration = parse_duration(text)
    quality = parse_quality(text)
    fmt = parse_format(text)
    audio_only = bool(re.search(r"\baudio.?only\b|\baudio\b", lower))

    options = RecordingOptions(
        duration_seconds=duration,
        quality=quality,
        output_format=fmt,
        audio_only=audio_only,
    )

    return ParsedCommand(
        intent=intent,
        url=url,
        job_id=job_id,
        options=options,
    )
