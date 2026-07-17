from urllib.parse import quote

from pydantic_settings import BaseSettings


class DbSettings(BaseSettings):
    """Database settings used to build a SQLAlchemy connection URL.

    ``SCHEMA`` has different meanings depending on the database type:

    - **SQLite** — file path (or ``:memory:``).
    - **MySQL / MariaDB** — the schema / database name in the URL path.
    - **PostgreSQL** — the *database* name in the URL path.  The PostgreSQL
      *schema* namespace within that database is controlled by ``PG_SCHEMA``
      (defaults to ``"public"``), which is always injected into the connection
      URL as ``options=-csearch_path=<value>``.
    """

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

    # PostgreSQL schema namespace inside the target database (search_path).
    # Defaults to "public" — PostgreSQL's built-in default schema.
    PG_SCHEMA: str = "public"

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

        base = f"{self.TYPE}+{self.DRIVER}://{credentials}{self.HOST}:{self.PORT}/{self.SCHEMA}"

        if self.is_postgresql:
            base = f"{base}?options=-csearch_path%3D{quote(self.PG_SCHEMA, safe='')}"

        return base

    @property
    def is_sqlite(self) -> bool:
        """Return whether the configured database dialect is SQLite."""

        return self.TYPE == "sqlite"

    @property
    def is_sqlite_memory(self) -> bool:
        """Return whether the configured database is an in-memory SQLite database."""

        return self.is_sqlite and self.SCHEMA == ":memory:"

    @property
    def is_postgresql(self) -> bool:
        """Return whether the configured database dialect is PostgreSQL."""

        return self.TYPE.lower() == "postgresql"
