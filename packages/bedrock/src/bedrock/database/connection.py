import json

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import DbSettings


def custom_json_serializer(obj: object) -> str:
    """Serialize values for JSON database columns."""

    return json.dumps(obj, default=str)


def build_engine(database_url: str, settings: DbSettings) -> Engine:
    """Create a SQLAlchemy engine for the provided database URL."""

    engine_kwargs: dict[str, object] = {
        "json_serializer": custom_json_serializer,
        "pool_pre_ping": True,
    }

    if not settings.is_sqlite:
        engine_kwargs["pool_recycle"] = settings.POOL_RECYCLE
        engine_kwargs["pool_timeout"] = settings.POOL_TIMEOUT
        engine_kwargs["pool_size"] = settings.POOL_SIZE

    return create_engine(database_url, **engine_kwargs)


def build_session_local(engine: Engine) -> sessionmaker[Session]:
    """Create a SQLAlchemy session factory bound to the given engine."""

    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def build_session_local_from_settings(
    settings: DbSettings,
    database_url: str | None = None,
) -> tuple[Engine, sessionmaker[Session]]:
    """Build the engine and session factory from resolved settings."""

    resolved_database_url = database_url or settings.SQLALCHEMY_DATABASE_URI
    engine = build_engine(resolved_database_url, settings)
    return engine, build_session_local(engine)
