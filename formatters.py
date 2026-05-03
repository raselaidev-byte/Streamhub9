"""
Formats Job state into Telegram-ready markdown messages.
"""

from __future__ import annotations

from core.models import Job, JobStatus


def format_job_status(job: Job) -> str:
    """Full job status card — used for progress updates and /status."""
    lines = [
        f"{job.status_emoji} *Job `{job.id}`*",
        f"📡 Status: `{job.status.value.upper()}`",
        f"🔗 URL: `{_truncate(job.stream_url, 50)}`",
    ]

    if job.stream_info:
        si = job.stream_info
        res = f"{si.width}x{si.height}" if si.width and si.height else "unknown"
        lines.append(f"📺 Stream: `{res}` | `{si.video_codec or '?'}/{si.audio_codec or '?'}` | `{si.bitrate_kbps or '?'} kbps`")

    lines.append("")

    # Progress block
    p = job.progress
    if job.status in (JobStatus.RECORDING, JobStatus.PAUSED, JobStatus.COMPLETED):
        lines.append(f"⏱ Elapsed: `{job.elapsed_human}`")
        lines.append(f"💾 Size: `{p.file_size_mb:.1f} MB`")
        if p.bitrate_kbps:
            lines.append(f"📶 Bitrate: `{p.bitrate_kbps:.0f} kbps`")
        if p.speed:
            lines.append(f"⚡ Speed: `{p.speed:.2f}x`")

    if job.status == JobStatus.COMPLETED:
        lines.append("")
        lines.append("✅ *Recording complete!*")
        if p.output_files:
            for f in p.output_files[:3]:
                lines.append(f"📁 `{f}`")
            if len(p.output_files) > 3:
                lines.append(f"_(+ {len(p.output_files) - 3} more files)_")

    elif job.status == JobStatus.FAILED:
        lines.append("")
        lines.append(f"❌ *Failed*: `{job.error_message or 'unknown error'}`")
        if job.retry_count > 0:
            lines.append(f"🔄 Retries: {job.retry_count}/{3}")

    elif job.status == JobStatus.QUEUED:
        lines.append("⏳ Waiting in queue…")

    # Options
    opts = job.options
    opt_parts = [f"quality=`{opts.quality}`", f"format=`{opts.output_format}`"]
    if opts.duration_seconds:
        h, rem = divmod(opts.duration_seconds, 3600)
        m, s = divmod(rem, 60)
        opt_parts.append(f"limit=`{h:02d}:{m:02d}:{s:02d}`")
    if opts.audio_only:
        opt_parts.append("`audio-only`")
    lines.append("")
    lines.append("⚙️ " + " | ".join(opt_parts))

    return "\n".join(lines)


def format_jobs_list(jobs: list[Job]) -> str:
    if not jobs:
        return "📭 No recording jobs found."
    lines = ["📋 *Your Recordings*\n"]
    for job in jobs:
        age = ""
        if job.started_at:
            from datetime import datetime, timezone
            delta = datetime.now(timezone.utc) - job.started_at
            age = f" | started {_human_delta(int(delta.total_seconds()))} ago"
        lines.append(
            f"{job.status_emoji} `{job.id}` — *{job.status.value}*{age}\n"
            f"   💾 {job.progress.file_size_mb:.1f} MB | ⏱ {job.elapsed_human}"
        )
        lines.append("")
    return "\n".join(lines)


def format_help() -> str:
    return (
        "🎬 *StreamBot — M3U8 Recorder*\n\n"
        "*Commands:*\n"
        "`/record <url>` — Start a new recording\n"
        "`/stop <job_id>` — Stop a recording\n"
        "`/pause <job_id>` — Pause a recording\n"
        "`/resume <job_id>` — Resume a paused recording\n"
        "`/status [job_id]` — Show job status\n"
        "`/list` — List all your recordings\n"
        "`/cancel <job_id>` — Cancel a queued job\n\n"
        "*Natural language also works:*\n"
        "_\"record https://... for 2 hours high quality\"_\n"
        "_\"start recording https://... audio only\"_\n\n"
        "*Quality options:* `original` `high` `medium` `low`\n"
        "*Formats:* `mp4` (default) `mkv` `ts`\n"
        "*Duration:* `1h`, `30m`, `90s`, `1:30`"
    )


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"


def _human_delta(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
