"""SQLAlchemy engine and session-factory construction."""

import importlib
import json

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import DatabaseConfigurationError, DbSettings

# Maps an async DBAPI driver to the importable module and the bedrock-core extra.
_ASYNC_DRIVER_EXTRAS: dict[str, tuple[str, str]] = {
    "aiosqlite": ("aiosqlite", "async-sqlite"),
    "asyncpg": ("asyncpg", "async-postgresql"),
    "psycopg": ("psycopg", "async-postgresql"),
    "aiomysql": ("aiomysql", "async-mysql"),
    "asyncmy": ("asyncmy", "async-mysql"),
}


def custom_json_serializer(obj: object) -> str:
    """Serialize values for JSON database columns."""

    return json.dumps(obj, default=str)


def _engine_kwargs(settings: DbSettings) -> dict[str, object]:
    """Return engine keyword arguments for *settings*.

    SQLite keeps ignoring pool settings. Every other dialect receives a pool
    whose checked-out connections cannot exceed ``MAX_CONNECTIONS``.
    """

    engine_kwargs: dict[str, object] = {
        "json_serializer": custom_json_serializer,
        "pool_pre_ping": True,
    }
    if not settings.is_sqlite:
        engine_kwargs["pool_recycle"] = settings.POOL_RECYCLE
        engine_kwargs["pool_timeout"] = settings.POOL_TIMEOUT
        engine_kwargs["pool_size"] = settings.POOL_SIZE
        engine_kwargs["max_overflow"] = settings.MAX_CONNECTIONS - settings.POOL_SIZE
    return engine_kwargs


def _missing_async_driver_detail(driver: str) -> str:
    """Describe how to install the package that provides *driver*."""

    package, extra = _ASYNC_DRIVER_EXTRAS.get(driver, (driver, None))
    if extra is None:
        return (
            f"Async database driver '{driver}' is not installed. "
            f"Install the '{package}' package before using DATABASE_ASYNC_DRIVER."
        )
    return (
        f"Async database driver '{driver}' is not installed. "
        f"Install the '{extra}' extra: pip install 'bedrock-core[{extra}]'."
    )


def ensure_async_driver_installed(driver: str) -> None:
    """Import the package that provides *driver*.

    Args:
        driver: Async DBAPI driver name, such as ``aiosqlite`` or ``asyncpg``.

    Raises:
        DatabaseConfigurationError: If the package is missing. Known drivers
            name the ``bedrock-core`` extra that installs them.
    """

    package, _extra = _ASYNC_DRIVER_EXTRAS.get(driver, (driver, None))
    try:
        importlib.import_module(package)
    except ImportError as exc:
        raise DatabaseConfigurationError(_missing_async_driver_detail(driver)) from exc


def build_engine(database_url: str, settings: DbSettings) -> Engine:
    """Create a SQLAlchemy engine for the provided database URL.

    Args:
        database_url: Rendered SQLAlchemy URL.
        settings: Pool and dialect settings. Non-SQLite engines receive
            ``pool_size``, ``pool_timeout``, ``pool_recycle``, and
            ``max_overflow = MAX_CONNECTIONS - POOL_SIZE``.

    Returns:
        A synchronous :class:`~sqlalchemy.engine.Engine`.
    """

    return create_engine(database_url, **_engine_kwargs(settings))


def async_url_for_driver(database_url: str, async_driver: str) -> str:
    """Replace only the DBAPI driver in an already-resolved database URL.

    The dialect, credentials, host, database, and query string stay as they were
    resolved for the synchronous engine. ``db.init(url=...)`` therefore points
    the async engine at that same database.

    Args:
        database_url: URL already chosen for the synchronous engine.
        async_driver: Async DBAPI name, such as ``aiosqlite`` or ``asyncpg``.

    Returns:
        The same URL with ``<dialect>+<async_driver>`` as its driver name.
        SQLite ``:memory:`` stays in the canonical unquoted form.
    """

    parsed = make_url(database_url)
    backend = parsed.get_backend_name()
    drivername = f"{backend}+{async_driver}"
    if backend == "sqlite" and parsed.database == ":memory:":
        return f"{drivername}:///:memory:"
    return parsed.set(drivername=drivername).render_as_string(hide_password=False)


def build_async_engine(settings: DbSettings, database_url: str | None = None) -> AsyncEngine:
    """Create an async engine for the URL the synchronous engine already uses.

    Args:
        settings: Pool settings and ``ASYNC_DRIVER``. Pool options still come
            from these settings. The URL does not.
        database_url: Resolved synchronous URL, including one passed to
            ``db.init(url=...)``. When omitted, the URL is rendered from
            *settings* and then given the async driver.

    Returns:
        An :class:`~sqlalchemy.ext.asyncio.AsyncEngine`.

    Raises:
        DatabaseConfigurationError: If ``ASYNC_DRIVER`` is unset or its package
            is not installed.
    """

    if not settings.ASYNC_DRIVER:
        raise DatabaseConfigurationError(
            "Async database access requires DATABASE_ASYNC_DRIVER. "
            "Set it to an async DBAPI driver such as 'aiosqlite', 'asyncpg', or 'asyncmy', "
            "and install the matching bedrock-core extra."
        )
    ensure_async_driver_installed(settings.ASYNC_DRIVER)
    resolved_url = (
        async_url_for_driver(database_url, settings.ASYNC_DRIVER)
        if database_url is not None
        else settings.database_uri(driver=settings.ASYNC_DRIVER)
    )
    try:
        return create_async_engine(resolved_url, **_engine_kwargs(settings))
    except ImportError as exc:
        raise DatabaseConfigurationError(_missing_async_driver_detail(settings.ASYNC_DRIVER)) from exc


def build_session_local(engine: Engine) -> sessionmaker[Session]:
    """Create a SQLAlchemy session factory bound to the given engine."""

    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def build_async_session_local(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create an async session factory bound to *engine*."""

    return async_sessionmaker(autocommit=False, autoflush=False, bind=engine)


def build_session_local_from_settings(
    settings: DbSettings,
    database_url: str | None = None,
) -> tuple[Engine, sessionmaker[Session]]:
    """Build the engine and session factory from resolved settings."""

    resolved_database_url = database_url or settings.SQLALCHEMY_DATABASE_URI
    engine = build_engine(resolved_database_url, settings)
    return engine, build_session_local(engine)
