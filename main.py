"""
Bot entry point.
Starts python-telegram-bot v20+ in polling mode.
Handles graceful shutdown on SIGTERM/SIGINT.
"""

from __future__ import annotations

import asyncio
import signal
import sys

from telegram.ext import Application

from bot.handlers import register_handlers
from config.settings import settings
from monitoring.logger import configure_logging, get_logger
from monitoring.metrics import monitor_disk_loop

log = get_logger(__name__)


async def _run_bot() -> None:
    log.info("bot_starting", token_prefix=settings.TELEGRAM_BOT_TOKEN[:10] + "…")

    app = (
        Application.builder()
        .token(settings.TELEGRAM_BOT_TOKEN)
        .read_timeout(30)
        .write_timeout(30)
        .connect_timeout(30)
        .pool_timeout(30)
        .build()
    )

    register_handlers(app)

    # Start disk monitor in background
    asyncio.create_task(monitor_disk_loop())

    # Set bot commands for the menu
    from telegram import BotCommand
    commands = [
        BotCommand("start",  "Start the bot"),
        BotCommand("record", "Record an M3U8 stream"),
        BotCommand("stop",   "Stop a recording"),
        BotCommand("pause",  "Pause a recording"),
        BotCommand("resume", "Resume a paused recording"),
        BotCommand("status", "Check job status"),
        BotCommand("list",   "List all recordings"),
        BotCommand("help",   "Show help"),
    ]
    await app.bot.set_my_commands(commands)
    log.info("bot_commands_set", count=len(commands))

    async with app:
        await app.start()
        log.info("bot_started", username=(await app.bot.get_me()).username)
        await app.updater.start_polling(
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=True,
        )

        # Wait until stop signal
        stop_event = asyncio.Event()

        def _on_signal(sig):
            log.info("shutdown_signal", sig=sig)
            stop_event.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _on_signal, sig.name)

        await stop_event.wait()

        log.info("bot_stopping")
        await app.updater.stop()
        await app.stop()

    log.info("bot_stopped")


def main() -> None:
    configure_logging()
    log.info("streambot_init", version="1.0.0", python=sys.version.split()[0])
    asyncio.run(_run_bot())


if __name__ == "__main__":
    main()
