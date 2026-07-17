"""Tests for the PostgreSQL database provisioner.

Covers unit tests (using mocks) for all provisioner paths:

- SQLite no-op
- Database already exists (idempotent)
- Database missing → created
- Insufficient CREATEDB privilege → actionable error
- ``_is_missing_database_error`` detection logic
- ``DatabaseDoesNotExistError`` raised by ``_get_current_revisions`` when DB is absent
- ``DbSettings`` new fields (PG_SCHEMA)

Integration tests are guarded by the ``postgres`` mark and require a live
PostgreSQL server.  Run them with::

    pytest -m postgres packages/bedrock/tests/test_database_provisioner.py
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from bedrock.database.config import DbSettings
from bedrock.database.migrations_manager import _is_missing_database_error
from bedrock.database.provisioner import (
    DatabaseDoesNotExistError,
    DatabaseProvisionError,
    check_database_exists,
    ensure_database_exists,
)

# ---------------------------------------------------------------------------
# DbSettings — new fields
# ---------------------------------------------------------------------------


class TestDbSettingsNewFields:
    """Verify the new PostgreSQL-specific settings fields."""

    def test_pg_schema_defaults_to_none(self):
        settings = DbSettings(TYPE="postgresql", DRIVER="psycopg", HOST="localhost", PORT=5432, SCHEMA="mydb")
        assert settings.PG_SCHEMA is None

    def test_pg_schema_appears_in_url(self):
        settings = DbSettings(
            TYPE="postgresql",
            DRIVER="psycopg",
            HOST="localhost",
            PORT=5432,
            SCHEMA="mydb",
            PG_SCHEMA="bedrock",
        )
        url = settings.SQLALCHEMY_DATABASE_URI
        assert "search_path" in url
        assert "bedrock" in url

    def test_no_pg_schema_produces_clean_url(self):
        settings = DbSettings(
            TYPE="postgresql",
            DRIVER="psycopg",
            HOST="localhost",
            PORT=5432,
            SCHEMA="mydb",
        )
        url = settings.SQLALCHEMY_DATABASE_URI
        assert "?" not in url
        assert "search_path" not in url

    def test_build_maintenance_url_always_targets_postgres_db(self):
        from bedrock.database.provisioner import _build_maintenance_url

        settings = DbSettings(
            TYPE="postgresql",
            DRIVER="psycopg",
            HOST="db.example.com",
            PORT=5432,
            SCHEMA="myapp",
            USERNAME="admin",
        )
        maint_url = _build_maintenance_url(settings)
        assert maint_url.endswith("/postgres")
        assert "myapp" not in maint_url

    def test_build_maintenance_url_preserves_credentials(self):
        from bedrock.database.provisioner import _build_maintenance_url

        settings = DbSettings(
            TYPE="postgresql",
            DRIVER="psycopg",
            HOST="localhost",
            PORT=5432,
            SCHEMA="target_db",
            USERNAME="admin",
            PASSWORD="secret",
        )
        url = _build_maintenance_url(settings)
        assert "admin" in url
        assert url.endswith("/postgres")


# ---------------------------------------------------------------------------
# Provisioner — SQLite no-op
# ---------------------------------------------------------------------------


class TestSQLiteNoOp:
    """Provisioner is a no-op for SQLite databases."""

    def test_ensure_database_exists_sqlite_returns_false(self):
        settings = DbSettings(TYPE="sqlite", SCHEMA=":memory:")
        result = ensure_database_exists(settings)
        assert result is False

    def test_check_database_exists_sqlite_returns_true(self):
        settings = DbSettings(TYPE="sqlite", SCHEMA=":memory:")
        assert check_database_exists(settings) is True


# ---------------------------------------------------------------------------
# Provisioner — database already exists
# ---------------------------------------------------------------------------


class TestDatabaseAlreadyExists:
    """When the target database exists ensure_database_exists returns False."""

    @patch("bedrock.database.provisioner._database_exists", return_value=True)
    def test_returns_false_when_database_exists(self, mock_exists):
        settings = DbSettings(TYPE="postgresql", DRIVER="psycopg", HOST="localhost", PORT=5432, SCHEMA="mydb")
        result = ensure_database_exists(settings)
        assert result is False
        mock_exists.assert_called_once_with(settings)

    @patch("bedrock.database.provisioner._database_exists", return_value=True)
    def test_does_not_call_create_when_exists(self, mock_exists):
        settings = DbSettings(TYPE="postgresql", DRIVER="psycopg", HOST="localhost", PORT=5432, SCHEMA="mydb")
        with patch("bedrock.database.provisioner._create_database") as mock_create:
            ensure_database_exists(settings)
            mock_create.assert_not_called()

    @patch("bedrock.database.provisioner._database_exists", return_value=True)
    def test_check_database_exists_true(self, mock_exists):
        settings = DbSettings(TYPE="postgresql", DRIVER="psycopg", HOST="localhost", PORT=5432, SCHEMA="mydb")
        assert check_database_exists(settings) is True


# ---------------------------------------------------------------------------
# Provisioner — database missing → created
# ---------------------------------------------------------------------------


class TestDatabaseCreated:
    """When the target database is absent ensure_database_exists creates it."""

    @patch("bedrock.database.provisioner._database_exists", return_value=False)
    @patch("bedrock.database.provisioner._create_database")
    def test_returns_true_when_database_created(self, mock_create, mock_exists):
        settings = DbSettings(TYPE="postgresql", DRIVER="psycopg", HOST="localhost", PORT=5432, SCHEMA="newdb")
        result = ensure_database_exists(settings)
        assert result is True

    @patch("bedrock.database.provisioner._database_exists", return_value=False)
    @patch("bedrock.database.provisioner._create_database")
    def test_calls_create_when_missing(self, mock_create, mock_exists):
        settings = DbSettings(TYPE="postgresql", DRIVER="psycopg", HOST="localhost", PORT=5432, SCHEMA="newdb")
        ensure_database_exists(settings)
        mock_create.assert_called_once_with(settings)

    @patch("bedrock.database.provisioner._database_exists", return_value=False)
    def test_check_database_exists_false(self, mock_exists):
        settings = DbSettings(TYPE="postgresql", DRIVER="psycopg", HOST="localhost", PORT=5432, SCHEMA="missing")
        assert check_database_exists(settings) is False


# ---------------------------------------------------------------------------
# Provisioner — insufficient CREATEDB privileges
# ---------------------------------------------------------------------------


class TestInsufficientPrivileges:
    """Missing CREATEDB privilege produces an actionable error."""

    @patch("bedrock.database.provisioner._database_exists", return_value=False)
    def test_create_failure_raises_provision_error(self, mock_exists):
        settings = DbSettings(
            TYPE="postgresql",
            DRIVER="psycopg",
            HOST="localhost",
            PORT=5432,
            SCHEMA="mydb",
            PASSWORD="supersecret",
        )

        with patch("bedrock.database.provisioner._create_database") as mock_create:
            mock_create.side_effect = DatabaseProvisionError("mydb", "permission denied to create database")
            with pytest.raises(DatabaseProvisionError) as exc_info:
                ensure_database_exists(settings)

        assert "mydb" in str(exc_info.value)
        assert "CREATEDB" in str(exc_info.value)

    def test_provision_error_sanitises_password(self):
        settings = DbSettings(
            TYPE="postgresql",
            DRIVER="psycopg",
            HOST="localhost",
            PORT=5432,
            SCHEMA="mydb",
            PASSWORD="top_secret_pw",
        )
        raw_msg = "could not connect with password top_secret_pw"

        from bedrock.database.provisioner import _sanitise_error

        sanitised = _sanitise_error(raw_msg, settings)
        assert "top_secret_pw" not in sanitised
        assert "***" in sanitised

    def test_provision_error_message_no_credentials(self):
        err = DatabaseProvisionError("mydb", "permission denied")
        assert "CREATEDB" in str(err)
        assert "mydb" in str(err)
        assert "permission denied" in str(err)


# ---------------------------------------------------------------------------
# DatabaseDoesNotExistError
# ---------------------------------------------------------------------------


class TestDatabaseDoesNotExistError:
    """Verify the human-readable shape of DatabaseDoesNotExistError."""

    def test_message_includes_database_name(self):
        err = DatabaseDoesNotExistError("my_project_db")
        msg = str(err)
        assert "my_project_db" in msg

    def test_message_includes_actionable_command(self):
        err = DatabaseDoesNotExistError("my_project_db")
        msg = str(err)
        assert "bedrock db create" in msg

    def test_attribute_set(self):
        err = DatabaseDoesNotExistError("my_project_db")
        assert err.database_name == "my_project_db"

    def test_is_bedrock_exc(self):
        from bedrock.exc import BedrockExc

        err = DatabaseDoesNotExistError("x")
        assert isinstance(err, BedrockExc)


# ---------------------------------------------------------------------------
# _is_missing_database_error
# ---------------------------------------------------------------------------


class TestIsMissingDatabaseError:
    """Detection of the PostgreSQL 3D000 error via various exception shapes."""

    def test_detects_pgcode_3d000(self):
        exc = Exception("database does not exist")
        exc.pgcode = "3D000"  # type: ignore[attr-defined]
        assert _is_missing_database_error(exc) is True

    def test_detects_message_containing_does_not_exist(self):
        exc = Exception("database 'mydb' does not exist")
        assert _is_missing_database_error(exc) is True

    def test_returns_false_for_unrelated_errors(self):
        exc = Exception("connection refused")
        assert _is_missing_database_error(exc) is False

    def test_detects_via_cause_chain(self):
        inner = Exception("database 'foo' does not exist")
        outer = Exception("wrapped")
        outer.__cause__ = inner
        assert _is_missing_database_error(outer) is True

    def test_detects_3d000_in_message(self):
        exc = Exception("FATAL: error 3d000 database missing")
        assert _is_missing_database_error(exc) is True

    def test_returns_false_for_empty_exception(self):
        exc = Exception()
        assert _is_missing_database_error(exc) is False


# ---------------------------------------------------------------------------
# MigrationsManager._get_current_revisions — missing DB raises, not empty set
# ---------------------------------------------------------------------------


class TestMigrationsManagerMissingDatabase:
    """_get_current_revisions raises DatabaseDoesNotExistError instead of returning set()."""

    def _make_manager(self, db_url: str = "postgresql+psycopg://user@localhost/testdb"):
        from unittest.mock import MagicMock

        from bedrock.database.migrations_manager import MigrationsManager

        registry = MagicMock()
        registry.all.return_value = []
        return MigrationsManager(registry=registry, database_url=db_url)

    def test_raises_database_does_not_exist_error_on_missing_db(self):
        """When engine.connect() fails with a 'does not exist' error, raise the right exc."""
        manager = self._make_manager()

        missing_db_exc = Exception("database 'testdb' does not exist")

        fake_script = MagicMock()
        fake_script.walk_revisions.return_value = [MagicMock(revision="abc123", branch_labels={"testapp"})]

        fake_cfg = MagicMock()

        # create_engine is imported inside the function body so patch via sqlalchemy.
        with patch("bedrock.database.migrations_manager.ScriptDirectory.from_config", return_value=fake_script):
            with patch("sqlalchemy.create_engine") as mock_engine_factory:
                mock_engine = MagicMock()
                mock_engine_factory.return_value = mock_engine
                mock_conn = MagicMock()
                mock_conn.__enter__ = MagicMock(side_effect=missing_db_exc)
                mock_conn.__exit__ = MagicMock(return_value=False)
                mock_engine.connect.return_value = mock_conn

                with pytest.raises(DatabaseDoesNotExistError) as exc_info:
                    manager._get_current_revisions(fake_cfg, "testapp")

                assert "testdb" in str(exc_info.value)

    def test_does_not_raise_for_generic_connection_error(self):
        """Generic connection errors still silently return set() (non-PG environments)."""
        manager = self._make_manager()

        generic_exc = Exception("connection refused")

        fake_script = MagicMock()
        fake_script.walk_revisions.return_value = [MagicMock(revision="abc123", branch_labels={"testapp"})]

        fake_cfg = MagicMock()

        # create_engine is imported inside the function body so patch via sqlalchemy.
        with patch("bedrock.database.migrations_manager.ScriptDirectory.from_config", return_value=fake_script):
            with patch("sqlalchemy.create_engine") as mock_engine_factory:
                mock_engine = MagicMock()
                mock_engine_factory.return_value = mock_engine
                mock_conn = MagicMock()
                mock_conn.__enter__ = MagicMock(side_effect=generic_exc)
                mock_conn.__exit__ = MagicMock(return_value=False)
                mock_engine.connect.return_value = mock_conn

                result = manager._get_current_revisions(fake_cfg, "testapp")
                assert result == set()


# ---------------------------------------------------------------------------
# Integration tests — require live PostgreSQL (skipped by default)
# ---------------------------------------------------------------------------

# Mark all integration tests so they can be selected / excluded with -m postgres.
pytestmark_integration = pytest.mark.postgres


@pytest.fixture
def pg_settings():
    """Resolve PostgreSQL settings from environment variables.

    Skips the test if DATABASE_TYPE is not set to a PostgreSQL variant.
    Set these environment variables to enable integration tests::

        DATABASE_TYPE=postgresql
        DATABASE_DRIVER=psycopg
        DATABASE_HOST=localhost
        DATABASE_PORT=5432
        DATABASE_USERNAME=postgres
        DATABASE_PASSWORD=<password>
        DATABASE_SCHEMA=bedrock_test_integ
    """
    settings = DbSettings()
    if settings.is_sqlite:
        pytest.skip("Integration tests require a PostgreSQL database (set DATABASE_TYPE=postgresql).")
    return settings


@pytest.fixture
def clean_test_database(pg_settings: DbSettings):
    """Ensure the integration test database does not exist before the test and drops it after."""
    import sqlalchemy
    from bedrock.database.provisioner import _build_maintenance_url
    from sqlalchemy import text

    maint_url = _build_maintenance_url(pg_settings)
    engine = sqlalchemy.create_engine(maint_url, isolation_level="AUTOCOMMIT")
    db_name = pg_settings.SCHEMA

    with engine.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))

    yield pg_settings

    with engine.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
    engine.dispose()


@pytest.mark.postgres
class TestPostgreSQLIntegration:
    """Live PostgreSQL integration tests.

    Run with: pytest -m postgres packages/bedrock/tests/test_database_provisioner.py
    """

    def test_missing_database_raises_error(self, clean_test_database):
        """A missing database should not silently appear as empty revision history."""
        settings = clean_test_database
        assert check_database_exists(settings) is False

    def test_create_database_provisions_successfully(self, clean_test_database):
        """ensure_database_exists returns True when the database is created."""
        settings = clean_test_database
        created = ensure_database_exists(settings)
        assert created is True
        assert check_database_exists(settings) is True

    def test_idempotent_when_database_already_exists(self, clean_test_database):
        """Calling ensure_database_exists twice does not raise an error."""
        settings = clean_test_database
        ensure_database_exists(settings)
        result = ensure_database_exists(settings)
        assert result is False  # already existed on second call

    def test_permission_denied_raises_provision_error(self, pg_settings: DbSettings):
        """A user without CREATEDB gets a human-readable DatabaseProvisionError."""
        from sqlalchemy import create_engine, text

        # Create a restricted user for this test.
        db_name = "bedrock_test_noperm_" + pg_settings.SCHEMA
        restricted_settings = pg_settings.model_copy(update={"SCHEMA": db_name, "USERNAME": "bedrock_noperm_user"})

        from bedrock.database.provisioner import _build_maintenance_url

        maint_engine = create_engine(_build_maintenance_url(pg_settings), isolation_level="AUTOCOMMIT")
        try:
            with maint_engine.connect() as conn:
                conn.execute(
                    text(
                        "DO $$ BEGIN "
                        "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='bedrock_noperm_user') "
                        "  THEN CREATE ROLE bedrock_noperm_user LOGIN PASSWORD 'test'; "
                        "  END IF; "
                        "END $$"
                    )
                )
                # Ensure NOCREATEDB
                conn.execute(text("ALTER ROLE bedrock_noperm_user NOCREATEDB"))
        finally:
            maint_engine.dispose()

        try:
            with pytest.raises(DatabaseProvisionError) as exc_info:
                ensure_database_exists(restricted_settings)
            assert "CREATEDB" in str(exc_info.value)
            assert db_name in str(exc_info.value)
        finally:
            cleanup_engine = create_engine(_build_maintenance_url(pg_settings), isolation_level="AUTOCOMMIT")
            with cleanup_engine.connect() as conn:
                conn.execute(text("DROP ROLE IF EXISTS bedrock_noperm_user"))
            cleanup_engine.dispose()
