"""
Telegram inline keyboards used across bot handlers.
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from core.models import Job, JobStatus


def job_control_keyboard(job: Job) -> InlineKeyboardMarkup:
    """Keyboard shown on active job progress messages."""
    buttons = []

    if job.status == JobStatus.RECORDING:
        buttons.append([
            InlineKeyboardButton("⏸ Pause", callback_data=f"pause:{job.id}"),
            InlineKeyboardButton("🛑 Stop",  callback_data=f"stop:{job.id}"),
        ])
    elif job.status == JobStatus.PAUSED:
        buttons.append([
            InlineKeyboardButton("▶️ Resume", callback_data=f"resume:{job.id}"),
            InlineKeyboardButton("🛑 Stop",   callback_data=f"stop:{job.id}"),
        ])
    elif job.status == JobStatus.QUEUED:
        buttons.append([
            InlineKeyboardButton("🚫 Cancel", callback_data=f"cancel:{job.id}"),
        ])

    buttons.append([
        InlineKeyboardButton("📊 Refresh", callback_data=f"status:{job.id}"),
    ])
    return InlineKeyboardMarkup(buttons)


def jobs_list_keyboard(jobs: list[Job]) -> InlineKeyboardMarkup:
    """Quick-access keyboard for /list command."""
    buttons = []
    for job in jobs[:8]:  # max 8 buttons
        label = f"{job.status_emoji} {job.id} — {job.status.value}"
        buttons.append([
            InlineKeyboardButton(label, callback_data=f"status:{job.id}")
        ])
    return InlineKeyboardMarkup(buttons)


def confirm_stop_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes, stop", callback_data=f"confirm_stop:{job_id}"),
            InlineKeyboardButton("❌ No",        callback_data=f"status:{job_id}"),
        ]
    ])
