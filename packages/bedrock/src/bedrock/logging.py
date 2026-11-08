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
# Loguru assigns its autoinit stderr handler this id and does not reuse it.
_LOGURU_DEFAULT_HANDLER_ID = 0


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
    installed a sink, only that handler is removed and replaced. Existing
    handlers, including Loguru's preconfigured stderr handler, stay in place.
    This function never calls :meth:`loguru.Logger.remove` without a handler id.

    Process entry points that own stderr, such as the Bedrock CLI, call
    :func:`configure_cli_logging` instead.

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
    _configure_logging(
        replace_default=False,
        level=level,
        format=format,
        file=file,
        rotation=rotation,
        retention=retention,
        compression=compression,
        serialize=serialize,
        backtrace=backtrace,
        diagnose=diagnose,
    )


def configure_cli_logging() -> None:
    """Install the CLI sink and retire Loguru's preconfigured stderr handler.

    Loguru autoinitialization leaves a stderr handler that ignores ``LOG_*``.
    Keeping it duplicates CLI lines and lets records through below
    ``LOG_LEVEL``. This removes that handler when it is still the original
    stderr sink, then installs Bedrock's sink. Handlers added by the host are
    left in place. Library code should call :func:`configure_logging`, which
    does not remove the preconfigured handler.
    """
    _configure_logging(replace_default=True)


def _configure_logging(
    *,
    replace_default: bool,
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
    """Resolve ``LOG_*`` settings and install Bedrock's sink.

    Args:
        replace_default: When true, remove Loguru's preconfigured stderr
            handler before installing Bedrock's sink.
        level: Logging level override.
        format: Format string override.
        file: Log file override.
        rotation: Rotation override.
        retention: Retention override.
        compression: Compression override.
        serialize: JSON serialization override.
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
        if replace_default:
            _remove_preconfigured_stderr_handler()

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


def _remove_preconfigured_stderr_handler() -> None:
    """Remove Loguru's autoinit stderr handler when that sink is still installed.

    Handler id 0 is the preconfigured sink when autoinitialization ran. A host
    sink only has that id when autoinitialization was disabled, and this
    function leaves that sink alone. The Bedrock handler is never removed here.
    """
    if _bedrock_handler_id == _LOGURU_DEFAULT_HANDLER_ID:
        return

    handler_id = _preconfigured_stderr_handler_id()
    if handler_id is None:
        return
    try:
        logger.remove(handler_id)
    except ValueError:
        return


def _preconfigured_stderr_handler_id() -> int | None:
    """Return handler id 0 when it is still Loguru's autoinit stderr sink."""
    from loguru import _defaults

    if not _defaults.LOGURU_AUTOINIT:
        return None

    handler = logger._core.handlers.get(_LOGURU_DEFAULT_HANDLER_ID)
    if handler is None or not _sink_targets_stderr(handler):
        return None
    return _LOGURU_DEFAULT_HANDLER_ID


def _sink_targets_stderr(handler: object) -> bool:
    """Return whether ``handler`` writes to the process stderr stream."""
    sink = getattr(handler, "_sink", None)
    stream = getattr(sink, "_stream", None)
    return _stream_is_stderr(stream)


def _stream_is_stderr(stream: object) -> bool:
    """Return whether ``stream`` is stderr or a wrapper around it."""
    if stream is sys.stderr or stream is sys.__stderr__:
        return True
    for attr in ("wrapped", "__wrapped__", "_StreamWrapper__wrapped"):
        wrapped = getattr(stream, attr, None)
        if wrapped is sys.stderr or wrapped is sys.__stderr__:
            return True
    return False


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


__all__ = ["LoggingSettings", "configure_cli_logging", "configure_logging", "get_logger", "logger"]
