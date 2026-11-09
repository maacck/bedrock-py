"""Tests for migration locks and the read-only db check command."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from bedrock.cli.db import db_app
from bedrock.database.migrations_manager import BranchRevisionStatus, MigrationError, MigrationsManager
from bedrock.module.registry import ModuleRegistry
from bedrock.testing.modules import ModuleFactory
from typer.testing import CliRunner


def _revision_a(name: str, rev: str = "mak111") -> str:
    """Root revision for the *name* branch."""

    return f'''"""init

Revision ID: {rev}a
Revises:
Create Date: 2026-01-01

"""

from alembic import op
import sqlalchemy as sa

revision = "{rev}a"
down_revision = None
branch_labels = ("{name}",)
depends_on = None


def upgrade() -> None:
    op.create_table("{name}", sa.Column("id", sa.Integer(), primary_key=True))


def downgrade() -> None:
    op.drop_table("{name}")
'''


def _revision_b(name: str, rev: str = "mak111") -> str:
    """Second revision for the *name* branch."""

    return f'''"""follow

Revision ID: {rev}b
Revises: {rev}a
Create Date: 2026-01-02

"""

from alembic import op
import sqlalchemy as sa

revision = "{rev}b"
down_revision = "{rev}a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("{name}", sa.Column("name", sa.String(length=50), nullable=True))


def downgrade() -> None:
    op.drop_column("{name}", "name")
'''


class _Result:
    """Scalar result returned by the lock-connection stub."""

    def __init__(self, value: object) -> None:
        self._value = value

    def scalar(self) -> object:
        """Return the scripted lock result."""

        return self._value


class _Conn:
    """Records lock statements without opening a database."""

    def __init__(self, value: object) -> None:
        self._value = value
        self.statements: list[str] = []

    def execute(self, statement: object, parameters: dict[str, object] | None = None) -> _Result:
        """Record the SQL text and return the scripted result."""

        self.statements.append(str(statement))
        return _Result(self._value)

    def commit(self) -> None:
        """Record a commit issued to keep the session-level lock."""

        self.statements.append("COMMIT")


def _manager(url: str, lock_timeout: float = 30) -> MigrationsManager:
    """Build a manager that does not need a populated registry for lock helpers."""

    return MigrationsManager(registry=ModuleRegistry(), database_url=url, lock_timeout=lock_timeout)


def _write_orders_app(factory: ModuleFactory, name: str, rev: str = "mak111") -> str:
    """Create an importable app with two revisions on one branch."""

    factory(
        name,
        manifest={"title": "Orders", "version": "0.1.0"},
        models=(
            "from sqlalchemy import Column, Integer\n"
            "from bedrock.database.base import BedrockModel\n\n"
            "class Order(BedrockModel):\n"
            f"    __tablename__ = '{name}'\n"
            "    id = Column(Integer, primary_key=True)\n"
        ),
    )
    migrations = factory.root / name / "migrations"
    migrations.mkdir()
    (migrations / f"{rev}a_init.py").write_text(_revision_a(name, rev), encoding="utf-8")
    (migrations / f"{rev}b_follow.py").write_text(_revision_b(name, rev), encoding="utf-8")
    return name


def _database_url(db_path: Path) -> str:
    """Return the sqlite URL DbSettings would build for an absolute path."""

    return f"sqlite+pysqlite:////{db_path.as_posix().lstrip('/')}"


class TestMigrationLockSelection:
    """Each dialect gets the lock statement that can serialize upgrade."""

    def test_postgresql_uses_an_advisory_lock(self) -> None:
        """PostgreSQL acquisition polls pg_try_advisory_lock."""

        manager = _manager("postgresql+psycopg2://app:secret@db.example:5432/app")
        connection = _Conn(True)

        manager._acquire_dialect_lock(connection, "postgresql")

        sql = " ".join(connection.statements)
        assert "pg_try_advisory_lock" in sql
        assert "COMMIT" in connection.statements

    def test_mysql_and_mariadb_use_get_lock(self) -> None:
        """MySQL and MariaDB acquisition calls GET_LOCK."""

        manager = _manager("mysql+pymysql://app:secret@db.example:3306/app")
        for dialect in ("mysql", "mariadb"):
            connection = _Conn(1)
            manager._acquire_dialect_lock(connection, dialect)
            assert any("GET_LOCK" in statement for statement in connection.statements)

    def test_sqlite_runs_unlocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SQLite migrations do not open a lock connection."""

        def fail_create(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("SQLite migrations must not open a lock connection")

        monkeypatch.setattr("bedrock.database.migrations_manager.create_engine", fail_create)
        manager = _manager("sqlite+pysqlite:///orders.db")

        with manager._migration_lock():
            pass

    def test_postgresql_timeout_raises_migration_error(self) -> None:
        """A failed advisory-lock wait is a Bedrock migration error."""

        manager = _manager("postgresql+psycopg2://app:secret@db.example:5432/app", lock_timeout=0)
        connection = _Conn(False)

        with pytest.raises(MigrationError, match="PostgreSQL migration lock"):
            manager._acquire_dialect_lock(connection, "postgresql")

    def test_mysql_timeout_raises_migration_error(self) -> None:
        """A failed GET_LOCK is a Bedrock migration error."""

        manager = _manager("mysql+pymysql://app:secret@db.example:3306/app", lock_timeout=0)
        connection = _Conn(0)

        with pytest.raises(MigrationError, match="MySQL migration lock"):
            manager._acquire_dialect_lock(connection, "mysql")

    def test_upgrade_and_downgrade_hold_the_lock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Upgrade and downgrade run inside the migration lock."""

        events: list[object] = []

        @contextmanager
        def lock(self: MigrationsManager) -> Iterator[None]:
            events.append("enter")
            yield
            events.append("exit")

        def upgrade(_cfg: object, target: str) -> None:
            events.append(("upgrade", target))

        def downgrade(_cfg: object, target: str) -> None:
            events.append(("downgrade", target))

        monkeypatch.setattr(MigrationsManager, "_migration_lock", lock)
        monkeypatch.setattr(MigrationsManager, "_get_app", lambda self, app: object())
        monkeypatch.setattr(MigrationsManager, "_build_config", lambda self, app: object())
        monkeypatch.setattr("bedrock.database.migrations_manager.command.upgrade", upgrade)
        monkeypatch.setattr("bedrock.database.migrations_manager.command.downgrade", downgrade)
        manager = _manager("sqlite+pysqlite:///orders.db")

        manager.upgrade("mak111_head")
        manager.downgrade("mak111_head", "-1")

        assert events == [
            "enter",
            ("upgrade", "mak111_head@head"),
            "exit",
            "enter",
            ("downgrade", "mak111_head@-1"),
            "exit",
        ]


class TestDbCheck:
    """db check reports branch state and exits non-zero when action is required."""

    def _invoke(
        self,
        monkeypatch: pytest.MonkeyPatch,
        module_factory: ModuleFactory,
        db_path: Path,
        *,
        upgrade_to: str | None,
        app_name: str,
    ) -> tuple[int, BranchRevisionStatus]:
        """Populate the orders app, optionally upgrade, and run ``db check``."""

        import bedrock.module.registry as registry_module

        import_path = _write_orders_app(module_factory, app_name)
        monkeypatch.setenv("DATABASE_TYPE", "sqlite")
        monkeypatch.setenv("DATABASE_DRIVER", "pysqlite")
        monkeypatch.setenv("DATABASE_SCHEMA", str(db_path))
        registry = ModuleRegistry()
        monkeypatch.setattr(registry_module, "apps", registry)
        registry.populate([import_path])
        manager = MigrationsManager(registry, _database_url(db_path))
        if upgrade_to is not None:
            manager.upgrade(import_path, upgrade_to)

        result = CliRunner().invoke(db_app, ["check", import_path])
        status = manager.check(import_path)[0]
        assert isinstance(status, BranchRevisionStatus)
        from bedrock.database.base import BedrockModel

        table = BedrockModel.metadata.tables.get(app_name)
        if table is not None:
            BedrockModel.metadata.remove(table)
        return result.exit_code, status

    def test_at_head_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch, module_factory: ModuleFactory, tmp_path: Path
    ) -> None:
        """A database at head exits 0."""

        exit_code, status = self._invoke(
            monkeypatch,
            module_factory,
            tmp_path / "head.db",
            upgrade_to="head",
            app_name="mak111_head",
        )

        assert status.state == "at-head"
        assert status.current == ("mak111b",)
        assert status.head == ("mak111b",)
        assert exit_code == 0

    def test_behind_exits_nonzero(
        self, monkeypatch: pytest.MonkeyPatch, module_factory: ModuleFactory, tmp_path: Path
    ) -> None:
        """A database on an older revision exits non-zero."""

        exit_code, status = self._invoke(
            monkeypatch,
            module_factory,
            tmp_path / "behind.db",
            upgrade_to="mak111a",
            app_name="mak111_behind",
        )

        assert status.state == "behind"
        assert status.current == ("mak111a",)
        assert status.head == ("mak111b",)
        assert exit_code != 0

    def test_uninitialized_exits_nonzero_without_mutating(
        self,
        monkeypatch: pytest.MonkeyPatch,
        module_factory: ModuleFactory,
        tmp_path: Path,
    ) -> None:
        """A database with no applied revision exits non-zero and stays uninitialized."""

        db_path = tmp_path / "empty.db"
        exit_code, status = self._invoke(
            monkeypatch,
            module_factory,
            db_path,
            upgrade_to=None,
            app_name="mak111_empty",
        )

        assert status.state == "unknown"
        assert status.current == ()
        assert status.head == ("mak111b",)
        assert exit_code != 0
        if db_path.exists():
            with sqlite3.connect(db_path) as connection:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            assert "alembic_version" not in tables


def _sqlite_rows(db_path: Path, query: str) -> set[str]:
    """Return the first column of *query* run against *db_path*."""

    with sqlite3.connect(db_path) as connection:
        return {row[0] for row in connection.execute(query)}


def _versions(db_path: Path) -> set[str]:
    return _sqlite_rows(db_path, "SELECT version_num FROM alembic_version")


def _tables(db_path: Path) -> set[str]:
    return _sqlite_rows(db_path, "SELECT name FROM sqlite_master WHERE type = 'table'")


@pytest.fixture
def populate(module_factory: ModuleFactory) -> Iterator[Callable[..., ModuleRegistry]]:
    """Write orders apps as ``(name, revision prefix)`` pairs and return a populated registry."""

    names: list[str] = []

    def _populate(*apps: tuple[str, str]) -> ModuleRegistry:
        created = [_write_orders_app(module_factory, name, rev) for name, rev in apps]
        names.extend(created)
        registry = ModuleRegistry()
        registry.populate(created)
        return registry

    yield _populate
    from bedrock.database.base import BedrockModel

    for name in names:
        table = BedrockModel.metadata.tables.get(name)
        if table is not None:
            BedrockModel.metadata.remove(table)


class TestMigrationTargets:
    """upgrade and downgrade only move the requested app's branch."""

    @pytest.fixture
    def two_apps(self, populate: Callable[..., ModuleRegistry], tmp_path: Path) -> tuple[MigrationsManager, Path]:
        registry = populate(("sec02_a", "sec02a"), ("sec02_b", "sec02b"))
        db_path = tmp_path / "two.db"
        manager = MigrationsManager(registry, _database_url(db_path))
        manager.upgrade("sec02_a")
        manager.upgrade("sec02_b")
        return manager, db_path

    def test_downgrade_to_base_leaves_other_branches(self, two_apps: tuple[MigrationsManager, Path]) -> None:
        manager, db_path = two_apps

        manager.downgrade("sec02_a", "base")

        assert _versions(db_path) == {"sec02bb"}
        assert "sec02_b" in _tables(db_path)
        assert "sec02_a" not in _tables(db_path)

    def test_relative_downgrade_moves_only_the_app_branch(self, two_apps: tuple[MigrationsManager, Path]) -> None:
        manager, db_path = two_apps

        manager.downgrade("sec02_a", "-1")

        assert _versions(db_path) == {"sec02aa", "sec02bb"}

    @pytest.mark.parametrize(
        ("method", "target"),
        [("downgrade", "sec02ba"), ("downgrade", "sec02_b@base"), ("upgrade", "heads"), ("upgrade", "sec02bb")],
    )
    def test_targets_outside_the_app_branch_are_rejected(
        self, two_apps: tuple[MigrationsManager, Path], method: str, target: str
    ) -> None:
        manager, db_path = two_apps

        with pytest.raises(MigrationError):
            getattr(manager, method)("sec02_a", target)

        assert _versions(db_path) == {"sec02ab", "sec02bb"}


def test_percent_encoded_database_url_reaches_alembic(populate: Callable[..., ModuleRegistry], tmp_path: Path) -> None:
    """URL-encoded characters (as in an encoded password) survive Alembic's config interpolation."""

    registry = populate(("sec03_app", "sec03"))
    db_path = tmp_path / "encoded.db"

    MigrationsManager(registry, f"{_database_url(db_path)}?timeout=%32%30").upgrade("sec03_app")

    assert _versions(db_path) == {"sec03b"}


def test_unreadable_database_is_an_error_not_an_empty_branch(
    populate: Callable[..., ModuleRegistry], tmp_path: Path
) -> None:
    """A failed read of applied revisions must not look like a branch that was never applied."""

    registry = populate(("sec10_app", "sec10"))
    manager = MigrationsManager(registry, _database_url(tmp_path / "missing" / "app.db"))

    with pytest.raises(MigrationError):
        manager.uninstall("sec10_app")
    with pytest.raises(MigrationError):
        manager.ensure_schema("sec10_app")
    assert manager.check("sec10_app")[0].state == "unknown"
