"""Tests for explicit, non-destructive Bedrock logging configuration."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import bedrock.logging as logging_mod
import pytest
from loguru import logger

_HOST_SINK_SCRIPT = textwrap.dedent(
    """
    from loguru import logger

    received = []

    def _sink(message):
        received.append(message.record["message"])

    logger.add(_sink)
    import bedrock
    from bedrock.logging import get_logger

    get_logger("host.app").info("host-sink-marker")
    logger.complete()
    if "host-sink-marker" not in received:
        raise SystemExit(f"host sink missed record: {received!r}")
    """
)

_HANDLER_SET_SCRIPT = textwrap.dedent(
    """
    from loguru import logger

    before = tuple(sorted(logger._core.handlers))
    import bedrock
    import bedrock.logging

    after = tuple(sorted(logger._core.handlers))
    if before != after:
        raise SystemExit(f"handlers changed: {before} -> {after}")
    """
)

_LOG_LEVEL_SCRIPT = textwrap.dedent(
    """
    import os
    from pathlib import Path

    os.environ["LOG_LEVEL"] = "DEBUG"
    import bedrock.logging as logging_mod

    os.environ["LOG_LEVEL"] = "ERROR"
    os.environ["LOG_FILE"] = os.environ["BEDROCK_TEST_LOG_PATH"]
    logging_mod.configure_logging()

    from loguru import logger

    logger.info("info-marker")
    logger.error("error-marker")
    logger.complete()
    text = Path(os.environ["LOG_FILE"]).read_text(encoding="utf-8")
    if "error-marker" not in text or "info-marker" in text:
        raise SystemExit(text)
    """
)

_CLI_SCRIPT = textwrap.dedent(
    """
    import sys

    sys.argv = ["bedrock", "run", "--app", "bedrock.contrib.cache"]
    from bedrock.cli.main import entrypoint

    entrypoint()
    """
)


def _run_isolated(code: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run ``code`` in a fresh interpreter so import side effects are observable."""
    child_env = os.environ.copy()
    for key in list(child_env):
        if key.startswith("LOG_"):
            del child_env[key]
    if env:
        child_env.update(env)
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=child_env,
        check=False,
        timeout=60,
    )


def _assert_success(result: subprocess.CompletedProcess[str]) -> None:
    """Fail with the child output when a subprocess assertion does not hold."""
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"


@pytest.fixture(autouse=True)
def _isolate_bedrock_handler() -> Iterator[None]:
    """Drop sinks this module installs so other tests keep their handlers."""

    def _drop_bedrock_handler() -> None:
        handler_id = logging_mod._bedrock_handler_id
        logging_mod._bedrock_handler_id = None
        if handler_id is None:
            return
        try:
            logger.remove(handler_id)
        except ValueError:
            pass

    _drop_bedrock_handler()
    before = set(logger._core.handlers)
    yield
    _drop_bedrock_handler()
    for handler_id in set(logger._core.handlers) - before:
        try:
            logger.remove(handler_id)
        except ValueError:
            pass


class TestImportDoesNotConfigureLogging:
    """Importing Bedrock leaves the host's loguru configuration alone."""

    def test_host_sink_receives_records_after_import(self) -> None:
        """A sink registered before import still receives records."""
        _assert_success(_run_isolated(_HOST_SINK_SCRIPT))

    def test_import_does_not_change_handler_set(self) -> None:
        """Importing ``bedrock`` and ``bedrock.logging`` does not add or remove handlers."""
        _assert_success(_run_isolated(_HANDLER_SET_SCRIPT))


class TestConfigureLogging:
    """``configure_logging`` owns only the sink it installed."""

    def test_reconfigure_replaces_only_bedrock_handler(self) -> None:
        """Two calls leave one Bedrock handler and do not remove host handlers."""
        received: list[str] = []

        def _sink(message: object) -> None:
            record = getattr(message, "record", None)
            if isinstance(record, dict):
                received.append(str(record["message"]))

        host_id = logger.add(_sink)
        before = set(logger._core.handlers)

        logging_mod.configure_logging()
        logging_mod.configure_logging()

        after = set(logger._core.handlers)
        assert host_id in after
        assert before - after == set()
        assert len(after - before) == 1

        logger.info("host-still-receives")
        logger.complete()
        assert "host-still-receives" in received

    def test_log_level_set_after_import_is_used(self, tmp_path: Path) -> None:
        """``LOG_LEVEL`` is read when configuring, not when the module is imported."""
        log_path = tmp_path / "bedrock.log"
        result = _run_isolated(
            _LOG_LEVEL_SCRIPT,
            env={"BEDROCK_TEST_LOG_PATH": str(log_path)},
        )
        _assert_success(result)


class TestCliEntrypoint:
    """The console-script entry point still emits Bedrock log output."""

    def test_entrypoint_emits_configured_log_line(self) -> None:
        """``entrypoint`` configures logging before the command runs."""
        result = _run_isolated(
            _CLI_SCRIPT,
            env={
                "LOG_LEVEL": "INFO",
                "LOG_FORMAT": "BEDROCK_CLI_LOG|{message}",
            },
        )
        _assert_success(result)
        assert "BEDROCK_CLI_LOG|Cache backend configured: memory" in result.stderr
