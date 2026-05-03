"""
Production-grade structured logging using structlog.
Outputs JSON in production, coloured console in dev.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from typing import Any

import structlog
from structlog.types import EventDict, WrappedLogger

from config.settings import settings


def _add_log_level(
    logger: WrappedLogger, method: str, event_dict: EventDict
) -> EventDict:
    """Add log level string to every event."""
    event_dict["level"] = method.upper()
    return event_dict


def _drop_color_message_key(
    logger: WrappedLogger, method: str, event_dict: EventDict
) -> EventDict:
    """Remove uvicorn colour-coded messages to keep logs clean."""
    event_dict.pop("color_message", None)
    return event_dict


def configure_logging() -> None:
    """Bootstrap structlog + stdlib logging once at startup."""

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        _add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        _drop_color_message_key,
    ]

    is_production = settings.LOG_FILE is not None

    if is_production:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=shared_processors
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    # console handler (always)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    handlers: list[logging.Handler] = [console_handler]

    # rotating file handler (production)
    if settings.LOG_FILE:
        file_handler = logging.handlers.RotatingFileHandler(
            filename=str(settings.LOG_FILE),
            maxBytes=settings.LOG_MAX_BYTES,
            backupCount=settings.LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    root_logger = logging.getLogger()
    root_logger.handlers = handlers
    root_logger.setLevel(settings.LOG_LEVEL.value)

    # Silence noisy third-party loggers
    for noisy in ("httpx", "httpcore", "telegram", "celery.utils.functional"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
