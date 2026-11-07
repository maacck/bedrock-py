"""Tests for database URL assembly, secrets, and pool limits."""

from __future__ import annotations

import time
from unittest.mock import Mock

import pytest
from bedrock.database.config import DatabaseConfigurationError, DbSettings
from bedrock.database.connection import build_engine
from pydantic import SecretStr, ValidationError
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.pool import QueuePool


def _postgres(**overrides: object) -> DbSettings:
    """Build non-SQLite settings, allowing pool fields to be overridden."""

    values: dict[str, object] = {
        "TYPE": "postgresql",
        "DRIVER": "psycopg2",
        "HOST": "db.example",
        "PORT": 5432,
        "USERNAME": "app",
        "PASSWORD": "super-secret-password",
        "SCHEMA": "app",
    }
    values.update(overrides)
    return DbSettings(**values)  # type: ignore[arg-type]


def _capture_engine(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Replace create_engine and return the kwargs it receives."""

    captured: dict[str, object] = {}

    def fake_create(url: str, **kwargs: object) -> Mock:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return Mock()

    monkeypatch.setattr("bedrock.database.connection.create_engine", fake_create)
    return captured


class _StubDBAPIConnection:
    """Minimal DBAPI connection for a QueuePool that never talks to a server."""

    def rollback(self) -> None:
        """Accept the pool's reset."""

    def close(self) -> None:
        """Accept the pool's termination."""


class TestPoolSettings:
    """Non-SQLite engines honor MAX_CONNECTIONS as the checkout ceiling."""

    def test_equal_ceiling_sets_max_overflow_to_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """POOL_SIZE == MAX_CONNECTIONS passes max_overflow 0."""

        captured = _capture_engine(monkeypatch)
        settings = _postgres(POOL_SIZE=4, MAX_CONNECTIONS=4, POOL_TIMEOUT=7, POOL_RECYCLE=90)

        build_engine(settings.SQLALCHEMY_DATABASE_URI, settings)

        kwargs = captured["kwargs"]
        assert isinstance(kwargs, dict)
        assert kwargs["pool_size"] == 4
        assert kwargs["max_overflow"] == 0
        assert kwargs["pool_timeout"] == 7
        assert kwargs["pool_recycle"] == 90

    def test_overflow_is_the_gap_under_the_ceiling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """max_overflow is MAX_CONNECTIONS - POOL_SIZE."""

        captured = _capture_engine(monkeypatch)
        settings = _postgres(POOL_SIZE=10, MAX_CONNECTIONS=15, POOL_TIMEOUT=12, POOL_RECYCLE=60)

        build_engine(settings.SQLALCHEMY_DATABASE_URI, settings)

        kwargs = captured["kwargs"]
        assert isinstance(kwargs, dict)
        assert kwargs["pool_size"] == 10
        assert kwargs["max_overflow"] == 5
        assert kwargs["pool_timeout"] == 12
        assert kwargs["pool_recycle"] == 60

    def test_sqlite_ignores_pool_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SQLite engine construction still omits the pool arguments."""

        captured = _capture_engine(monkeypatch)
        settings = DbSettings(TYPE="sqlite", DRIVER="pysqlite", SCHEMA=":memory:", POOL_SIZE=2, MAX_CONNECTIONS=4)

        build_engine(settings.SQLALCHEMY_DATABASE_URI, settings)

        kwargs = captured["kwargs"]
        assert isinstance(kwargs, dict)
        assert "pool_size" not in kwargs
        assert "max_overflow" not in kwargs
        assert "pool_timeout" not in kwargs
        assert "pool_recycle" not in kwargs

    def test_max_connections_below_pool_size_is_rejected(self) -> None:
        """A ceiling under the steady pool is a configuration error naming both settings."""

        with pytest.raises(DatabaseConfigurationError) as exc_info:
            _postgres(POOL_SIZE=10, MAX_CONNECTIONS=9)

        message = str(exc_info.value)
        assert "DATABASE_MAX_CONNECTIONS" in message
        assert "DATABASE_POOL_SIZE" in message
        assert "super-secret-password" not in message

    @pytest.mark.parametrize(
        ("pool_size", "max_connections"),
        [(1, 1), (1, 2)],
    )
    def test_queue_pool_blocks_until_timeout(self, pool_size: int, max_connections: int) -> None:
        """A QueuePool refuses checkouts past MAX_CONNECTIONS until POOL_TIMEOUT."""

        settings = _postgres(POOL_SIZE=pool_size, MAX_CONNECTIONS=max_connections, POOL_TIMEOUT=1)
        pool = QueuePool(
            _StubDBAPIConnection,
            pool_size=settings.POOL_SIZE,
            max_overflow=settings.MAX_CONNECTIONS - settings.POOL_SIZE,
            timeout=settings.POOL_TIMEOUT,
        )
        held = []
        try:
            held = [pool.connect() for _ in range(settings.MAX_CONNECTIONS)]
            started = time.monotonic()
            with pytest.raises(SATimeoutError):
                pool.connect()
            assert time.monotonic() - started >= 0.9
        finally:
            for connection in held:
                connection.close()
            pool.dispose()


class TestSecretUrl:
    """Passwords stay out of settings output and survive URL quoting."""

    def test_repr_and_validation_errors_hide_the_password(self) -> None:
        """repr(DbSettings) and validation errors do not contain the password."""

        settings = _postgres()
        rendered = settings.SQLALCHEMY_DATABASE_URI

        assert isinstance(settings.PASSWORD, SecretStr)
        assert "super-secret-password" not in repr(settings)
        assert rendered not in repr(settings)

        with pytest.raises(ValidationError) as exc_info:
            DbSettings(
                TYPE="postgresql",
                DRIVER="psycopg2",
                HOST="db.example",
                PORT="not-a-port",  # type: ignore[arg-type]
                USERNAME="app",
                PASSWORD="super-secret-password",
                SCHEMA="app",
            )

        assert "super-secret-password" not in str(exc_info.value)

    def test_reserved_characters_round_trip(self) -> None:
        """Usernames and passwords with reserved characters decode back to the originals."""

        username = "user@name:x/y"
        password = "p@ss:w/ord%"
        settings = _postgres(USERNAME=username, PASSWORD=password)

        parsed = make_url(settings.SQLALCHEMY_DATABASE_URI)

        assert parsed.username == username
        assert parsed.password == password
        assert parsed.host == "db.example"
        assert parsed.port == 5432
        assert parsed.database == "app"

    @pytest.mark.parametrize(
        ("schema", "expected"),
        [
            (":memory:", "sqlite+pysqlite:///:memory:"),
            ("bedrock.db", "sqlite+pysqlite:///bedrock.db"),
            ("/tmp/bedrock.db", "sqlite+pysqlite:////tmp/bedrock.db"),
        ],
    )
    def test_sqlite_urls_keep_their_historical_shape(self, schema: str, expected: str) -> None:
        """SQLite file and memory URLs stay compatible with the previous builder."""

        settings = DbSettings(TYPE="sqlite", DRIVER="pysqlite", SCHEMA=schema)

        assert settings.SQLALCHEMY_DATABASE_URI == expected

    def test_missing_host_names_the_settings_and_hides_the_password(self) -> None:
        """A non-SQLite URL without a host raises a Bedrock configuration error."""

        settings = DbSettings(TYPE="postgresql", DRIVER="psycopg2", PASSWORD="super-secret-password", SCHEMA="app")

        with pytest.raises(DatabaseConfigurationError) as exc_info:
            _ = settings.SQLALCHEMY_DATABASE_URI

        message = str(exc_info.value)
        assert "DATABASE_HOST" in message
        assert "DATABASE_PORT" in message
        assert "super-secret-password" not in message
