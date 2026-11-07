"""Loguru helpers for Bedrock.

Importing this module does not add, remove, or replace loguru handlers.
Process entry points that want Bedrock's sink call :func:`configure_logging`.
Library code should only call :func:`get_logger`.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Literal

from loguru import logger
from loguru._logger import Logger
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | <level>{message}</level>"
)

_bedrock_handler_id: int | None = None
_config_lock = threading.Lock()


class LoggingSettings(BaseSettings):
    """Logging settings read from ``LOG_*`` environment variables.

    A new instance is created each time :func:`configure_logging` runs, so
    values assigned after Bedrock is imported still take effect.

    Attributes:
        LEVEL: Loguru level name. ``LOG_LEVEL``, default ``INFO``.
        FORMAT: Loguru format string. ``LOG_FORMAT``.
        FILE: Optional log file path. ``LOG_FILE``. Unset logs to stderr only.
        ROTATION: Rotation condition passed to loguru when ``FILE`` is set.
        RETENTION: How long rotated files are kept.
        COMPRESSION: Compression used for rotated files.
        SERIALIZE: Whether loguru serializes records as JSON. ``LOG_SERIALIZE``.
    """

    model_config = SettingsConfigDict(env_prefix="LOG_", extra="ignore")

    LEVEL: str = "INFO"
    FORMAT: str = _DEFAULT_LOG_FORMAT
    FILE: str | None = None
    ROTATION: str = "500 MB"
    RETENTION: str = "7 days"
    COMPRESSION: str = "zip"
    SERIALIZE: bool = False


def configure_logging(
    level: str | None = None,
    format: str | None = None,
    file: str | Path | None = None,
    rotation: str | None = None,
    retention: str | None = None,
    compression: str | None = None,
    serialize: bool | None = None,
    backtrace: bool = True,
    diagnose: bool = False,
) -> None:
    """Install Bedrock's log sink without removing handlers owned by the host.

    ``LOG_*`` settings are resolved on this call. When Bedrock has already
    installed a sink, only that handler is removed and replaced. This function
    never calls :meth:`loguru.Logger.remove` without a handler id.

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        format: Log message format string.
        file: Path to log file. When omitted, ``LOG_FILE`` is used, and logs
            go to stderr if that is unset.
        rotation: Max size before the log file is rotated.
        retention: How long to keep old log files.
        compression: Compression format for rotated files.
        serialize: Whether to serialize logs to JSON.
        backtrace: Whether to show the full stack trace for exceptions.
        diagnose: Whether to show variable values in tracebacks.
    """
    global _bedrock_handler_id

    settings = LoggingSettings()
    log_level = level or settings.LEVEL
    log_format = format or settings.FORMAT
    log_file = file or settings.FILE
    log_rotation = rotation or settings.ROTATION
    log_retention = retention or settings.RETENTION
    log_compression = compression or settings.COMPRESSION
    log_serialize = serialize if serialize is not None else settings.SERIALIZE

    sink: str | Path | Literal["stderr"] = sys.stderr
    file_kwargs: dict[str, str] = {}
    if log_file:
        sink = Path(log_file)
        sink.parent.mkdir(parents=True, exist_ok=True)
        file_kwargs = {
            "rotation": log_rotation,
            "retention": log_retention,
            "compression": log_compression,
        }

    with _config_lock:
        handler_id = _bedrock_handler_id
        _bedrock_handler_id = None
        if handler_id is not None:
            try:
                logger.remove(handler_id)
            except ValueError:
                pass

        _bedrock_handler_id = logger.add(
            sink=sink,
            level=log_level,
            format=log_format,
            serialize=log_serialize,
            backtrace=backtrace,
            diagnose=diagnose,
            enqueue=True,
            **file_kwargs,
        )


def get_logger(name: str | None = None) -> Logger:
    """Return the process loguru logger, optionally bound to ``name``.

    Args:
        name: Optional name for the logger (typically ``__name__``).

    Returns:
        The shared loguru logger. Passing ``name`` binds that name onto the record.
    """
    if name:
        return logger.bind(name=name)
    return logger


__all__ = ["LoggingSettings", "configure_logging", "get_logger", "logger"]
