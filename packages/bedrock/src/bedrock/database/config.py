"""Database settings and the connection URL built from them."""

from typing import Self

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings
from sqlalchemy.engine import URL

from ..exc import ImproperlyConfigured


class DatabaseConfigurationError(ImproperlyConfigured):
    """Raised when database settings cannot be used to build an engine."""

    detail: str = "Database configuration is invalid."


class DbSettings(BaseSettings):
    """Database settings used to build a SQLAlchemy connection URL.

    ``PASSWORD`` is a :class:`~pydantic.SecretStr`. It is omitted from
    ``repr`` and from validation errors. Read the plain value with
    ``PASSWORD.get_secret_value()`` only at the edge that opens a connection.

    ``MAX_CONNECTIONS`` is the total number of connections one process may
    check out. Non-SQLite engines use ``max_overflow = MAX_CONNECTIONS -
    POOL_SIZE``. SQLite engines ignore the pool settings.

    Attributes:
        TYPE: SQLAlchemy dialect name, such as ``sqlite`` or ``postgresql``.
        DRIVER: Synchronous DBAPI driver name.
        ASYNC_DRIVER: Optional asynchronous DBAPI driver name. When unset,
            async session APIs are unavailable.
        HOST: Database host. Required for non-SQLite dialects.
        PORT: Database port. Required for non-SQLite dialects.
        USERNAME: Database user.
        PASSWORD: Database password, stored as a secret.
        MAX_CONNECTIONS: Per-process connection ceiling, including overflow.
        POOL_TIMEOUT: Seconds to wait for a pooled connection.
        POOL_SIZE: Steady-state pool size; must be at least 1.
        POOL_RECYCLE: Seconds before a pooled connection is recycled.
        MIGRATION_LOCK_TIMEOUT: Seconds to wait for the migration lock.
        SCHEMA: Database name, or a SQLite file path.
    """

    model_config = {"env_prefix": "DATABASE_", "hide_input_in_errors": True}

    TYPE: str = "sqlite"
    DRIVER: str = "pysqlite"
    ASYNC_DRIVER: str | None = None
    HOST: str | None = None
    PORT: int | None = None
    USERNAME: str | None = None
    PASSWORD: SecretStr | None = None
    MAX_CONNECTIONS: int = 10
    POOL_TIMEOUT: int = 30
    POOL_SIZE: int = 10
    POOL_RECYCLE: int = 1800
    MIGRATION_LOCK_TIMEOUT: float = 30
    SCHEMA: str = "bedrock.db"

    @model_validator(mode="after")
    def _validate_limits(self) -> Self:
        """Reject pool and lock settings that cannot be applied."""

        if self.POOL_SIZE < 1:
            raise DatabaseConfigurationError(
                "DATABASE_POOL_SIZE must be at least 1; SQLAlchemy treats 0 as an unlimited pool, which would "
                f"ignore DATABASE_MAX_CONNECTIONS (DATABASE_POOL_SIZE={self.POOL_SIZE})."
            )
        if self.MAX_CONNECTIONS < self.POOL_SIZE:
            raise DatabaseConfigurationError(
                "DATABASE_MAX_CONNECTIONS must be greater than or equal to DATABASE_POOL_SIZE "
                f"(DATABASE_MAX_CONNECTIONS={self.MAX_CONNECTIONS}, DATABASE_POOL_SIZE={self.POOL_SIZE})."
            )
        if self.MIGRATION_LOCK_TIMEOUT < 0:
            raise DatabaseConfigurationError(
                "DATABASE_MIGRATION_LOCK_TIMEOUT must be greater than or equal to 0 "
                f"(DATABASE_MIGRATION_LOCK_TIMEOUT={self.MIGRATION_LOCK_TIMEOUT})."
            )
        return self

    def sqlalchemy_url(self, *, driver: str | None = None) -> URL:
        """Build a SQLAlchemy URL, quoting every component.

        Args:
            driver: DBAPI driver name. Defaults to :attr:`DRIVER`.

        Returns:
            The URL for these settings. SQLite ``:memory:`` is still rendered
            by :attr:`SQLALCHEMY_DATABASE_URI` as the canonical unquoted form,
            because :meth:`URL.create` percent-encodes the colon.

        Raises:
            DatabaseConfigurationError: If a non-SQLite URL has no host or port.
        """

        resolved_driver = self.DRIVER if driver is None else driver
        drivername = f"{self.TYPE}+{resolved_driver}"
        if self.is_sqlite:
            return URL.create(drivername, database=self.SCHEMA)

        if self.HOST is None or self.PORT is None:
            raise DatabaseConfigurationError(
                "DATABASE_HOST and DATABASE_PORT must be configured for non-SQLite databases."
            )

        password = None if self.PASSWORD is None else self.PASSWORD.get_secret_value()
        return URL.create(
            drivername=drivername,
            username=self.USERNAME,
            password=password,
            host=self.HOST,
            port=self.PORT,
            database=self.SCHEMA,
        )

    def database_uri(self, *, driver: str | None = None) -> str:
        """Render the connection URL for *driver*.

        Args:
            driver: DBAPI driver name. Defaults to :attr:`DRIVER`.

        Returns:
            A SQLAlchemy URL string. The password is included so an engine can
            authenticate; it is not part of the settings ``repr``.
        """

        resolved_driver = self.DRIVER if driver is None else driver
        if self.is_sqlite and self.SCHEMA == ":memory:":
            return f"{self.TYPE}+{resolved_driver}:///:memory:"
        return self.sqlalchemy_url(driver=resolved_driver).render_as_string(hide_password=False)

    @property
    def SQLALCHEMY_DATABASE_URI(self) -> str:
        """Build the SQLAlchemy database URL from the configured settings."""

        return self.database_uri()

    @property
    def is_sqlite(self) -> bool:
        """Return whether the configured database dialect is SQLite."""

        return self.TYPE == "sqlite"

    @property
    def is_sqlite_memory(self) -> bool:
        """Return whether the configured database is an in-memory SQLite database."""

        return self.is_sqlite and self.SCHEMA == ":memory:"
