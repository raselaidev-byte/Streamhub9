"""
Telegram bot handlers.
Each /command and inline callback is handled here.
All heavy work is delegated to Celery tasks — handlers stay fast.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot.command_parser import ParsedCommand, extract_url, is_m3u8_url, parse_command
from bot.formatters import format_help, format_job_status, format_jobs_list
from bot.keyboards import confirm_stop_keyboard, job_control_keyboard, jobs_list_keyboard
from bot.middleware import can_start_job, check_rate_limit, is_blocked
from config.settings import settings
from core.job_manager import async_job_manager
from core.models import Job, JobPriority, JobStatus, RecordingOptions
from monitoring.logger import get_logger
from workers.recording_worker import (
    pause_recording,
    record_stream,
    resume_recording,
    stop_recording,
)

log = get_logger(__name__)

# ── Guard decorators / helpers ────────────────────────────────────────────────


async def _guard(update: Update) -> bool:
    """
    Returns True if the user is allowed to proceed.
    Sends a rejection message and returns False otherwise.
    """
    user = update.effective_user
    if not user:
        return False

    if is_blocked(user.id):
        await update.effective_message.reply_text("🚫 You are blocked from using this bot.")
        return False

    if not settings.is_allowed(user.id):
        await update.effective_message.reply_text("🔒 This bot is private. Access denied.")
        return False

    allowed, retry = check_rate_limit(user.id)
    if not allowed:
        await update.effective_message.reply_text(
            f"🚦 Slow down! Try again in {retry}s."
        )
        return False

    return True


def _user_id(update: Update) -> int:
    return update.effective_user.id  # type: ignore[union-attr]


def _chat_id(update: Update) -> int:
    return update.effective_chat.id  # type: ignore[union-attr]


async def _edit_or_reply(update: Update, text: str, **kwargs) -> None:
    """Edit existing message if callback, otherwise send new reply."""
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, **kwargs)
        except BadRequest:
            pass
    else:
        await update.effective_message.reply_text(text, **kwargs)


# ── /start, /help ─────────────────────────────────────────────────────────────


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    await update.message.reply_text(
        format_help(),
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    await update.message.reply_text(format_help(), parse_mode=ParseMode.MARKDOWN)


# ── /record ───────────────────────────────────────────────────────────────────


async def cmd_record(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return

    user_id = _user_id(update)
    args = " ".join(ctx.args or [])
    full_text = f"record {args}"

    parsed = parse_command(full_text)
    url = parsed.url

    if not url:
        await update.message.reply_text(
            "❌ Please provide an M3U8 URL.\n\nExample:\n`/record https://stream.example.com/live.m3u8`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if not is_m3u8_url(url):
        await update.message.reply_text(
            "⚠️ URL doesn't look like a stream. I'll try anyway…",
        )

    await _enqueue_recording(update, user_id, url, parsed.options)


async def handle_natural_record(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle natural language recording requests (non-command messages)."""
    if not await _guard(update):
        return

    text = update.message.text or ""
    parsed = parse_command(text)

    if parsed.intent != "record" or not parsed.url:
        return  # Not a record request — ignore

    await _enqueue_recording(update, _user_id(update), parsed.url, parsed.options)


async def _enqueue_recording(
    update: Update,
    user_id: int,
    url: str,
    options: RecordingOptions,
) -> None:
    # Check job quota
    ok, reason = can_start_job(user_id)
    if not ok:
        await update.effective_message.reply_text(reason)
        return

    # Build job
    priority = JobPriority.HIGH if settings.is_vip(user_id) else JobPriority.NORMAL
    job = Job(
        user_id=user_id,
        chat_id=_chat_id(update),
        stream_url=url,
        options=options,
        priority=priority,
    )

    # Persist to Redis
    await async_job_manager.save(job)

    # Send progress placeholder message (we'll edit this with updates)
    msg = await update.effective_message.reply_text(
        format_job_status(job),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=job_control_keyboard(job),
    )

    # Attach message_id so worker can push updates
    job = job.model_copy(update={"message_id": msg.message_id})
    await async_job_manager.save(job)

    # Enqueue Celery task
    queue = "priority" if settings.is_vip(user_id) else "normal"
    result = record_stream.apply_async(
        kwargs={"job_id": job.id},
        queue=queue,
        priority=priority.value,
    )

    # Save Celery task ID
    job = job.model_copy(update={"celery_task_id": result.id})
    await async_job_manager.save(job)

    log.info(
        "job_enqueued",
        job_id=job.id,
        user_id=user_id,
        url=url,
        celery_task=result.id,
        queue=queue,
    )

    # Launch background progress updater
    asyncio.create_task(
        _progress_update_loop(
            update.get_bot(),
            _chat_id(update),
            msg.message_id,
            job.id,
        )
    )


# ── Progress update loop ──────────────────────────────────────────────────────


async def _progress_update_loop(bot, chat_id: int, message_id: int, job_id: str) -> None:
    """
    Background coroutine that edits the progress message every N seconds.
    Exits when job reaches a terminal state.
    """
    while True:
        await asyncio.sleep(settings.PROGRESS_UPDATE_INTERVAL)
        job = await async_job_manager.get(job_id)
        if not job:
            break
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=format_job_status(job),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=job_control_keyboard(job) if not job.is_terminal else None,
            )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                log.warning("progress_edit_failed", job_id=job_id, error=str(e))
        except Exception:
            log.exception("progress_update_error", job_id=job_id)

        if job.is_terminal:
            break


# ── /stop ─────────────────────────────────────────────────────────────────────


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    job_id = (ctx.args or [""])[0].upper()
    await _do_stop(update, job_id)


async def _do_stop(update: Update, job_id: str) -> None:
    if not job_id:
        await update.effective_message.reply_text(
            "Usage: `/stop <JOB_ID>`", parse_mode=ParseMode.MARKDOWN
        )
        return

    job = await async_job_manager.get(job_id)
    if not job:
        await update.effective_message.reply_text(f"❌ Job `{job_id}` not found.")
        return

    if _user_id(update) != job.user_id and not settings.is_admin(_user_id(update)):
        await update.effective_message.reply_text("🔒 Not your job.")
        return

    if job.is_terminal:
        await update.effective_message.reply_text(
            f"Job `{job_id}` is already {job.status.value}."
        )
        return

    stop_recording.apply_async(kwargs={"job_id": job_id}, queue="priority")
    await update.effective_message.reply_text(
        f"🛑 Stop signal sent to job `{job_id}`.",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── /pause ────────────────────────────────────────────────────────────────────


async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    job_id = (ctx.args or [""])[0].upper()
    await _do_pause(update, job_id)


async def _do_pause(update: Update, job_id: str) -> None:
    if not job_id:
        await update.effective_message.reply_text("Usage: `/pause <JOB_ID>`", parse_mode=ParseMode.MARKDOWN)
        return

    job = await async_job_manager.get(job_id)
    if not job:
        await update.effective_message.reply_text(f"❌ Job `{job_id}` not found.")
        return

    if job.status != JobStatus.RECORDING:
        await update.effective_message.reply_text(f"Job `{job_id}` is not recording.")
        return

    pause_recording.apply_async(kwargs={"job_id": job_id}, queue="priority")
    await update.effective_message.reply_text(f"⏸ Pausing job `{job_id}`…", parse_mode=ParseMode.MARKDOWN)


# ── /resume ───────────────────────────────────────────────────────────────────


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    job_id = (ctx.args or [""])[0].upper()
    await _do_resume(update, job_id)


async def _do_resume(update: Update, job_id: str) -> None:
    if not job_id:
        await update.effective_message.reply_text("Usage: `/resume <JOB_ID>`", parse_mode=ParseMode.MARKDOWN)
        return

    job = await async_job_manager.get(job_id)
    if not job:
        await update.effective_message.reply_text(f"❌ Job `{job_id}` not found.")
        return

    if job.status != JobStatus.PAUSED:
        await update.effective_message.reply_text(f"Job `{job_id}` is not paused.")
        return

    resume_recording.apply_async(kwargs={"job_id": job_id}, queue="priority")
    await update.effective_message.reply_text(f"▶️ Resuming job `{job_id}`…", parse_mode=ParseMode.MARKDOWN)


# ── /status ───────────────────────────────────────────────────────────────────


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return

    job_id = (ctx.args or [""])[0].upper()
    if job_id:
        job = await async_job_manager.get(job_id)
        if not job:
            await update.message.reply_text(f"❌ Job `{job_id}` not found.")
            return
        await update.message.reply_text(
            format_job_status(job),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=job_control_keyboard(job) if not job.is_terminal else None,
        )
    else:
        jobs = await async_job_manager.list_user_jobs(_user_id(update))
        active = [j for j in jobs if j.is_active]
        if not active:
            await update.message.reply_text("📭 No active jobs.")
            return
        await update.message.reply_text(
            format_jobs_list(active),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=jobs_list_keyboard(active),
        )


# ── /list ─────────────────────────────────────────────────────────────────────


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update):
        return
    jobs = await async_job_manager.list_user_jobs(_user_id(update))
    await update.message.reply_text(
        format_jobs_list(jobs),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=jobs_list_keyboard(jobs) if jobs else None,
    )


# ── Inline callback handler ───────────────────────────────────────────────────


async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    parts = data.split(":", 1)
    action = parts[0]
    job_id = parts[1].upper() if len(parts) > 1 else ""

    if action == "stop":
        await query.edit_message_reply_markup(reply_markup=confirm_stop_keyboard(job_id))

    elif action == "confirm_stop":
        await _do_stop(update, job_id)

    elif action == "pause":
        await _do_pause(update, job_id)

    elif action == "resume":
        await _do_resume(update, job_id)

    elif action == "cancel":
        job = await async_job_manager.get(job_id)
        if job and job.status == JobStatus.QUEUED:
            await async_job_manager.update_status(job_id, JobStatus.CANCELLED)
            await query.edit_message_text(f"🚫 Job `{job_id}` cancelled.", parse_mode=ParseMode.MARKDOWN)

    elif action == "status":
        job = await async_job_manager.get(job_id)
        if job:
            try:
                await query.edit_message_text(
                    format_job_status(job),
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=job_control_keyboard(job) if not job.is_terminal else None,
                )
            except BadRequest:
                pass


# ── Error handler ─────────────────────────────────────────────────────────────


async def handle_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("telegram_error", error=str(ctx.error), exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            "⚠️ An internal error occurred. Please try again."
        )


# ── Registration ──────────────────────────────────────────────────────────────


def register_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(CommandHandler("record", cmd_record))
    app.add_handler(CommandHandler("stop",   cmd_stop))
    app.add_handler(CommandHandler("pause",  cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("list",   cmd_list))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_natural_record,
        )
    )
    app.add_error_handler(handle_error)
