from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Literal

from loguru import logger

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FORMAT = os.getenv(
    "LOG_FORMAT",
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | <level>{message}</level>",
)
LOG_FILE = os.getenv("LOG_FILE", None)
LOG_ROTATION = os.getenv("LOG_ROTATION", "500 MB")
LOG_RETENTION = os.getenv("LOG_RETENTION", "7 days")
LOG_COMPRESSION = os.getenv("LOG_COMPRESSION", "zip")
LOG_SERIALIZE = os.getenv("LOG_SERIALIZE", "false").lower() in ("true", "1", "yes")


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
    """Configure loguru logger with unified format and environment variable support.

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        format: Log message format string
        file: Path to log file (if None, logs to stderr only)
        rotation: Max size before log file is rotated
        retention: How long to keep old log files
        compression: Compression format for rotated files
        serialize: Whether to serialize logs to JSON
        backtrace: Whether to show full stack trace for exceptions
        diagnose: Whether to show variable values in tracebacks
    """
    logger.remove()

    log_level = level or LOG_LEVEL
    log_format = format or LOG_FORMAT
    log_file = file or LOG_FILE
    log_rotation = rotation or LOG_ROTATION
    log_retention = retention or LOG_RETENTION
    log_compression = compression or LOG_COMPRESSION
    log_serialize = serialize if serialize is not None else LOG_SERIALIZE

    sink: str | Path | Literal["stderr"] = sys.stderr
    file_kwargs: dict = {}
    if log_file:
        sink = Path(log_file)
        sink.parent.mkdir(parents=True, exist_ok=True)
        file_kwargs = {
            "rotation": log_rotation,
            "retention": log_retention,
            "compression": log_compression,
        }

    logger.add(
        sink=sink,
        level=log_level,
        format=log_format,
        serialize=log_serialize,
        backtrace=backtrace,
        diagnose=diagnose,
        enqueue=True,
        **file_kwargs,
    )


def get_logger(name: str | None = None) -> logger:
    """Get a logger instance.

    Args:
        name: Optional name for the logger (typically __name__)

    Returns:
        Configured loguru logger instance
    """
    if name:
        return logger.bind(name=name)
    return logger


configure_logging()

__all__ = ["configure_logging", "get_logger", "logger"]
