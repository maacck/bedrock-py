from urllib.parse import quote

from pydantic_settings import BaseSettings


class DbSettings(BaseSettings):
    """Database settings used to build a SQLAlchemy connection URL."""

    model_config = {"env_prefix": "DATABASE_"}

    TYPE: str = "sqlite"
    DRIVER: str = "pysqlite"
    HOST: str | None = None
    PORT: int | None = None
    USERNAME: str | None = None
    PASSWORD: str | None = None
    MAX_CONNECTIONS: int = 10
    POOL_TIMEOUT: int = 30
    POOL_SIZE: int = 10
    POOL_RECYCLE: int = 1800
    SCHEMA: str = "bedrock.db"

    @property
    def SQLALCHEMY_DATABASE_URI(self) -> str:
        """Build the SQLAlchemy database URL from the configured settings."""

        if self.TYPE == "sqlite":
            if self.SCHEMA == ":memory:":
                return f"{self.TYPE}+{self.DRIVER}:///{self.SCHEMA}"
            if self.SCHEMA.startswith("/"):
                return f"{self.TYPE}+{self.DRIVER}:////{self.SCHEMA.lstrip('/')}"
            return f"{self.TYPE}+{self.DRIVER}:///{self.SCHEMA}"

        if self.HOST is None or self.PORT is None:
            raise ValueError("HOST and PORT must be configured for non-sqlite databases.")

        credentials = ""
        if self.USERNAME is not None:
            credentials = self.USERNAME
            if self.PASSWORD is not None:
                credentials = f"{credentials}:{quote(self.PASSWORD)}"
            credentials = f"{credentials}@"

        return f"{self.TYPE}+{self.DRIVER}://{credentials}{self.HOST}:{self.PORT}/{self.SCHEMA}"

    @property
    def is_sqlite(self) -> bool:
        """Return whether the configured database dialect is SQLite."""

        return self.TYPE == "sqlite"

    @property
    def is_sqlite_memory(self) -> bool:
        """Return whether the configured database is an in-memory SQLite database."""

        return self.is_sqlite and self.SCHEMA == ":memory:"
