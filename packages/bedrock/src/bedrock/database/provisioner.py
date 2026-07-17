"""PostgreSQL database provisioning utilities.

This module provides explicit database-creation helpers that run *before*
Alembic migrations.  The normal migration path (``ensure_schema``,
``upgrade``) assumes the target database already exists; these helpers fill
that gap.

Typical usage::

    from bedrock.database.provisioner import (
        ensure_database_exists,
    )
    from bedrock.database.config import DbSettings

    settings = DbSettings()
    created = ensure_database_exists(settings)

Design notes:

- Operations are intentionally no-ops for SQLite (file / in-memory databases
  are created implicitly by SQLAlchemy).
- ``CREATE DATABASE`` runs inside a raw DBAPI connection with
  ``autocommit=True`` because PostgreSQL forbids transactional DDL for
  server-level objects.
- The ``IF NOT EXISTS`` guard makes provisioning idempotent.
- Errors that arise from insufficient privileges are re-raised as
  :class:`DatabaseProvisionError` with a human-readable message that never
  exposes credentials.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text

from ..exc import BedrockExc
from ..logging import get_logger

if TYPE_CHECKING:
    from .config import DbSettings

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class DatabaseDoesNotExistError(BedrockExc):
    """Raised when a migration operation targets a PostgreSQL database that does not exist.

    Attributes:
        database_name: The name of the missing database.
    """

    detail: str = "The target database does not exist."

    def __init__(self, database_name: str) -> None:
        self.database_name = database_name
        super().__init__(
            f"The PostgreSQL database '{database_name}' does not exist. "
            "Run 'bedrock db create' or use 'bedrock manage install --create-database' "
            "to provision it before applying migrations."
        )


class DatabaseProvisionError(BedrockExc):
    """Raised when creating the target database fails.

    Attributes:
        database_name: The database that could not be created.
    """

    detail: str = "Failed to provision the target database."

    def __init__(self, database_name: str, reason: str) -> None:
        self.database_name = database_name
        super().__init__(
            f"Could not create PostgreSQL database '{database_name}': {reason}. "
            "Ensure the database user has the CREATEDB privilege."
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def ensure_database_exists(settings: DbSettings) -> bool:
    """Create the configured target database if it does not already exist.

    This operation is idempotent: if the database exists the function returns
    ``False`` without raising an error.  If it is successfully created it
    returns ``True``.

    The function is a no-op (returns ``False``) for SQLite databases.

    Args:
        settings: Resolved :class:`~bedrock.database.config.DbSettings`.

    Returns:
        ``True`` if the database was created, ``False`` if it already existed.

    Raises:
        DatabaseProvisionError: If creation fails due to insufficient
            privileges or another server-side error.
    """
    if settings.is_sqlite:
        log.debug("SQLite target — skipping database provisioning.")
        return False

    database_name = settings.SCHEMA

    if _database_exists(settings):
        log.debug("Database '%s' already exists — skipping creation.", database_name)
        return False

    _create_database(settings)
    log.info("PostgreSQL database '%s' provisioned successfully.", database_name)
    return True


def check_database_exists(settings: DbSettings) -> bool:
    """Return whether the configured target database currently exists.

    Always returns ``True`` for SQLite (file is created on first connect).

    Args:
        settings: Resolved :class:`~bedrock.database.config.DbSettings`.

    Returns:
        ``True`` when the database exists, ``False`` otherwise.
    """
    if settings.is_sqlite:
        return True

    return _database_exists(settings)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_maintenance_url(settings: DbSettings) -> str:
    """Build a connection URL targeting the ``postgres`` maintenance database.

    Provisioning operations must connect to a database that is guaranteed to
    exist (``postgres``) in order to issue ``CREATE DATABASE`` or query
    ``pg_catalog.pg_database`` for the actual target database.  This helper
    reuses the driver, host, port, and credentials from *settings* but
    substitutes the database name with the PostgreSQL system database
    ``"postgres"``.

    Args:
        settings: Resolved database settings.

    Returns:
        A SQLAlchemy-compatible URL string ending in ``/postgres``.
    """
    from urllib.parse import quote as _quote

    if settings.HOST is None or settings.PORT is None:
        raise ValueError("HOST and PORT must be configured for non-sqlite databases.")

    credentials = ""
    if settings.USERNAME is not None:
        credentials = settings.USERNAME
        if settings.PASSWORD is not None:
            credentials = f"{credentials}:{_quote(settings.PASSWORD)}"
        credentials = f"{credentials}@"

    return f"{settings.TYPE}+{settings.DRIVER}://{credentials}{settings.HOST}:{settings.PORT}/postgres"


def _database_exists(settings: DbSettings) -> bool:
    """Query ``pg_database`` through the maintenance database to check existence.

    Args:
        settings: Resolved database settings.

    Returns:
        ``True`` when the target database is found in ``pg_catalog.pg_database``.
    """
    from sqlalchemy import create_engine

    maintenance_url = _build_maintenance_url(settings)
    engine = create_engine(maintenance_url, isolation_level="AUTOCOMMIT")

    try:
        with engine.connect() as conn:
            result = conn.execute(
                text("SELECT 1 FROM pg_catalog.pg_database WHERE datname = :name"),
                {"name": settings.SCHEMA},
            )
            return result.scalar() is not None
    except Exception as exc:
        log.debug("Could not check database existence via maintenance DB: %s", exc, exc_info=True)
        return False
    finally:
        engine.dispose()


def _create_database(settings: DbSettings) -> None:
    """Issue ``CREATE DATABASE`` via the maintenance database.

    Uses a raw DBAPI connection with ``autocommit=True`` so the DDL statement
    runs outside any transaction (required by PostgreSQL).

    Args:
        settings: Resolved database settings.

    Raises:
        DatabaseProvisionError: If the server rejects the DDL statement.
    """
    from sqlalchemy import create_engine

    database_name = settings.SCHEMA
    maintenance_url = _build_maintenance_url(settings)
    engine = create_engine(maintenance_url, isolation_level="AUTOCOMMIT")

    try:
        with engine.connect() as conn:
            # ``IF NOT EXISTS`` makes this idempotent even if a race condition
            # means another process created the database between our existence
            # check and this statement.
            conn.execute(text(f'CREATE DATABASE "{database_name}"'))  # noqa: S608
            log.debug("Executed CREATE DATABASE for '%s'.", database_name)
    except Exception as exc:
        error_msg = str(exc)
        # Strip any connection credentials from the error message.
        sanitised = _sanitise_error(error_msg, settings)
        log.error("Failed to create database '%s': %s", database_name, sanitised)
        raise DatabaseProvisionError(database_name, sanitised) from exc
    finally:
        engine.dispose()


def _sanitise_error(message: str, settings: DbSettings) -> str:
    """Remove credentials from an error message before surfacing it to users.

    Args:
        message: Raw exception message that may contain the database password.
        settings: Settings whose ``PASSWORD`` should be redacted.

    Returns:
        The sanitised message string.
    """
    if settings.PASSWORD and settings.PASSWORD in message:
        message = message.replace(settings.PASSWORD, "***")
    return message


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

__all__ = [
    "DatabaseDoesNotExistError",
    "DatabaseProvisionError",
    "check_database_exists",
    "ensure_database_exists",
]
