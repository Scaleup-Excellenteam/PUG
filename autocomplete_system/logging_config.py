"""Structured, real-time, size-rotated operational logging."""

from __future__ import annotations

import json
import logging
import sys
import threading
import traceback
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType
from typing import Any


LOGGER_NAMESPACE = "autocomplete"
SYSTEM_LOG_FILENAME = "system.jsonl"
DEFAULT_MAX_LOG_BYTES = 10 * 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT = 5
DEFAULT_LOG_DIRECTORY = Path(__file__).resolve().parent.parent / "logs"

_configuration_lock = threading.Lock()


class JsonLineFormatter(logging.Formatter):
    """Serialize one complete LogRecord as one UTF-8 JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, ZoneInfo("Asia/Jerusalem")
            ).strftime("%Y/%m/%d, %H:%M"),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", "log_message"),
            "message": record.getMessage(),
            "process_id": record.process,
            "thread_id": record.thread,
            "thread_name": record.threadName,
        }
        details = getattr(record, "details", None)
        if isinstance(details, dict):
            payload["details"] = details
        if record.exc_info:
            payload["exception"] = "".join(
                traceback.format_exception(*record.exc_info)
            ).rstrip()
        elif record.exc_text:
            payload["exception"] = record.exc_text
        return json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )


def configure_system_logging(
    log_directory: Path = DEFAULT_LOG_DIRECTORY,
    *,
    max_bytes: int = DEFAULT_MAX_LOG_BYTES,
    backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
    force: bool = False,
    install_exception_hooks: bool = True,
) -> Path:
    """Configure the shared application logger and return the active log path."""

    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive.")
    if backup_count < 0:
        raise ValueError("backup_count cannot be negative.")

    directory = Path(log_directory).resolve()
    path = directory / SYSTEM_LOG_FILENAME
    logger = logging.getLogger(LOGGER_NAMESPACE)
    with _configuration_lock:
        configured_handler = next(
            (
                handler
                for handler in logger.handlers
                if getattr(handler, "_autocomplete_system_handler", False)
            ),
            None,
        )
        if configured_handler is not None and not force:
            return Path(configured_handler.baseFilename)
        if force:
            _remove_and_close_handlers(logger)

        directory.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=False,
        )
        handler.setFormatter(JsonLineFormatter())
        handler._autocomplete_system_handler = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False

        if install_exception_hooks:
            _install_exception_hooks()

    log_event(
        logger,
        "logging_configured",
        log_path=str(path),
        max_bytes=max_bytes,
        backup_count=backup_count,
    )
    return path


def _remove_and_close_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.flush()
        handler.close()


def shutdown_system_logging() -> None:
    """Flush and close every application-owned operational log handler."""

    with _configuration_lock:
        _remove_and_close_handlers(logging.getLogger(LOGGER_NAMESPACE))


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    message: str | None = None,
    exc_info: bool | tuple[type[BaseException], BaseException, TracebackType] = False,
    **details: object,
) -> None:
    """Write one structured event; handlers flush after every emitted record."""

    logger.log(
        level,
        message or event.replace("_", " "),
        extra={"event": event, "details": details},
        exc_info=exc_info,
    )


def _install_exception_hooks() -> None:
    def log_uncaught_exception(
        exception_type: type[BaseException],
        exception: BaseException,
        trace: TracebackType | None,
    ) -> None:
        if issubclass(exception_type, KeyboardInterrupt):
            sys.__excepthook__(exception_type, exception, trace)
            return
        log_event(
            logging.getLogger(f"{LOGGER_NAMESPACE}.uncaught"),
            "uncaught_exception",
            level=logging.CRITICAL,
            exception_type=exception_type.__name__,
            exception_message=str(exception),
            exc_info=(exception_type, exception, trace) if trace else False,
        )

    def log_uncaught_thread_exception(args: threading.ExceptHookArgs) -> None:
        log_event(
            logging.getLogger(f"{LOGGER_NAMESPACE}.thread"),
            "uncaught_thread_exception",
            level=logging.CRITICAL,
            thread_name=args.thread.name if args.thread else None,
            exception_type=args.exc_type.__name__,
            exception_message=str(args.exc_value),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback)
            if args.exc_traceback
            else False,
        )

    sys.excepthook = log_uncaught_exception
    threading.excepthook = log_uncaught_thread_exception
