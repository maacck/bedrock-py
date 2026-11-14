"""Tests for migration locks and the read-only db check command."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from bedrock.cli.db import db_app
from bedrock.database.migrations_manager import BranchRevisionStatus, MigrationError, MigrationsManager
from bedrock.module.registry import ModuleRegistry
from bedrock.testing.modules import ModuleFactory
from typer.testing import CliRunner


@pytest.fixture(autouse=True)
def isolated_migration_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep models collected by other suites outside these migration databases."""
    from bedrock.database.base import BedrockModel

    monkeypatch.setattr(BedrockModel, "metadata", sa.MetaData())


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


def _model_source(class_name: str, table: str, extra: str = "") -> str:
    """Define a model inside the temporary app that owns its table."""
    return (
        "from sqlalchemy import Column, Integer, String, ForeignKey, Table\n"
        "from bedrock.database.base import BedrockModel\n\n"
        f"class {class_name}(BedrockModel):\n"
        f"    __tablename__ = {table!r}\n"
        "    id = Column(Integer, primary_key=True)\n"
        f"{extra}"
    )


def _migration_app(factory: ModuleFactory, name: str, models: str, files: dict[str, str] | None = None) -> str:
    """Create a model-owning app with an empty, stampable initial revision."""
    return factory(
        name,
        models=models,
        files={
            "migrations/initial.py": (
                f"revision = '{name}0'\n"
                "down_revision = None\n"
                f"branch_labels = ('{name}',)\n"
                "depends_on = None\n"
                "def upgrade():\n    pass\n"
                "def downgrade():\n    pass\n"
            ),
            **(files or {}),
        },
    )


class TestTableOwnership:
    """First installation creates only the tables actually owned by its app."""

    def test_first_install_includes_declared_association_table(
        self, module_factory: ModuleFactory, tmp_path: Path
    ) -> None:
        core = _migration_app(module_factory, "ow_core", _model_source("Account", "account"))
        plugin = _migration_app(
            module_factory,
            "ow_plugin",
            "from sqlalchemy import Table, Column, Integer, ForeignKey\n"
            "from bedrock.database.base import BedrockModel\n"
            "links = Table('links', BedrockModel.metadata,\n"
            "    Column('user_id', Integer, ForeignKey('account.id'), primary_key=True),\n"
            "    info={'bedrock_module': 'ow_plugin'})\n",
        )
        other = _migration_app(module_factory, "ow_other", _model_source("Other", "other"))
        registry = ModuleRegistry()
        registry.populate([core, plugin, other])
        db_path = tmp_path / "owned.db"
        manager = MigrationsManager(registry, _database_url(db_path))
        manager.ensure_schema(core)
        manager.ensure_schema(plugin)

        assert _tables(db_path) == {"account", "links", "alembic_version"}
        assert _versions(db_path) == {"ow_core0", "ow_plugin0"}

    def test_first_install_uses_definition_owner(self, module_factory: ModuleFactory, tmp_path: Path) -> None:
        core = _migration_app(module_factory, "ow_core", _model_source("Account", "account"))
        plugin = _migration_app(
            module_factory,
            "ow_plugin",
            _model_source("Tag", "tag") + "from ow_core.models import Account as ImportedAccount\n",
        )
        registry = ModuleRegistry()
        registry.populate([core, plugin])
        db_path = tmp_path / "imported.db"
        MigrationsManager(registry, _database_url(db_path)).ensure_schema(plugin)
        assert _tables(db_path) == {"tag", "alembic_version"}
        assert _versions(db_path) == {"ow_plugin0"}

    def test_first_install_includes_loaded_domain_models(self, module_factory: ModuleFactory, tmp_path: Path) -> None:
        parent = _migration_app(
            module_factory,
            "ow_app",
            "from .domain.models import Domain\nfrom .child.models import Child\n",
            files={
                "domain/__init__.py": "",
                "domain/models.py": _model_source("Domain", "domain_table"),
                "child/__init__.py": "",
                "child/manifest.yaml": "title: Child\nversion: '1'\n",
                "child/models.py": _model_source("Child", "child_table"),
            },
        )
        neighbor = _migration_app(module_factory, "ow_application", _model_source("Neighbor", "neighbor"))
        registry = ModuleRegistry()
        registry.populate([parent, "ow_app.child", neighbor])
        db_path = tmp_path / "domains.db"
        MigrationsManager(registry, _database_url(db_path)).ensure_schema(parent)
        assert _tables(db_path) == {"domain_table", "alembic_version"}

    @pytest.mark.parametrize(
        "case", ["missing", "nonstring", "empty", "unknown", "no_models", "conflict", "shared", "unloaded"]
    )
    def test_invalid_ownership_prevents_first_install(
        self, module_factory: ModuleFactory, tmp_path: Path, case: str
    ) -> None:
        other = module_factory("ow_other")
        if case in {"missing", "nonstring", "empty", "unknown", "no_models"}:
            info = {
                "missing": {},
                "nonstring": {"bedrock_module": 1},
                "empty": {"bedrock_module": ""},
                "unknown": {"bedrock_module": "ow_missing"},
                "no_models": {"bedrock_module": other},
            }[case]
            models = (
                "from sqlalchemy import Table, Column, Integer\n"
                "from bedrock.database.base import BedrockModel\n"
                f"invalid = Table('invalid', BedrockModel.metadata, Column('id', Integer), info={info!r})\n"
            )
        else:
            models = _model_source("Invalid", "invalid")
            if case == "conflict":
                models += "Invalid.__table__.info['bedrock_module'] = 'ow_other'\n"
            elif case == "shared":
                models += "class Foreign(Invalid):\n    __module__ = 'ow_other.models'\n"
            else:
                models += "Invalid.__module__ = 'unloaded.models'\n"
        app = _migration_app(module_factory, "ow_invalid", models)
        registry = ModuleRegistry()
        registry.populate([app, other])
        db_path = tmp_path / "invalid.db"
        with pytest.raises(MigrationError, match="invalid"):
            MigrationsManager(registry, _database_url(db_path)).ensure_schema(app)
        assert _tables(db_path) == set()

    def test_empty_models_do_not_create_foreign_tables(self, module_factory: ModuleFactory, tmp_path: Path) -> None:
        core = _migration_app(module_factory, "ow_core", _model_source("Account", "account"))
        empty = _migration_app(module_factory, "ow_empty", "")
        registry = ModuleRegistry()
        registry.populate([core, empty])
        db_path = tmp_path / "empty.db"
        MigrationsManager(registry, _database_url(db_path)).ensure_schema(empty)
        assert _tables(db_path) == {"alembic_version"}
        assert _versions(db_path) == {"ow_empty0"}

    def test_inheritance_keeps_table_owners(self, module_factory: ModuleFactory, tmp_path: Path) -> None:
        parent = _migration_app(
            module_factory,
            "ow_parent",
            _model_source("Parent", "parent") + "class LocalChild(Parent):\n    pass\n",
        )
        child = _migration_app(
            module_factory,
            "ow_child",
            "from sqlalchemy import Column, ForeignKey, Integer\n"
            "from ow_parent.models import Parent\n"
            "class Child(Parent):\n"
            "    __tablename__ = 'child'\n"
            "    id = Column(Integer, ForeignKey('parent.id'), primary_key=True)\n",
        )
        registry = ModuleRegistry()
        registry.populate([parent, child])
        db_path = tmp_path / "inheritance.db"
        manager = MigrationsManager(registry, _database_url(db_path))
        manager.ensure_schema(parent)
        assert _tables(db_path) == {"parent", "alembic_version"}
        manager.ensure_schema(child)
        assert _tables(db_path) == {"parent", "child", "alembic_version"}

    @pytest.mark.parametrize("operation", ["revision", "ensure_schema"])
    @pytest.mark.parametrize("explicit_owner", [False, True])
    def test_shared_table_requires_resolved_models_or_explicit_owner(
        self, module_factory: ModuleFactory, tmp_path: Path, operation: str, explicit_owner: bool
    ) -> None:
        core = _migration_app(
            module_factory,
            "ow_core",
            _model_source(
                "Account",
                "account",
                "    __table_args__ = {'info': {'bedrock_module': 'ow_core'}}\n" if explicit_owner else "",
            )
            + "from ow_extension.models import ExtendedAccount\n",
        )
        extension = module_factory(
            "ow_extension",
            models=(
                "from sqlalchemy import Column, String\n"
                "from ow_core.models import Account\n"
                "class ExtendedAccount(Account):\n"
                "    extension_value = Column(String(30))\n"
            ),
        )
        registry = ModuleRegistry()
        registry.populate([core])
        db_path = tmp_path / "shared.db"
        manager = MigrationsManager(registry, _database_url(db_path))
        scripts = module_factory.root / core / "migrations"
        if operation == "revision":
            command.stamp(manager._build_config(core), "heads")

        def migrate() -> None:
            if operation == "revision":
                manager.revision(core, "add account")
            else:
                manager.ensure_schema(core)

        if not explicit_owner:
            with pytest.raises(MigrationError, match="ow_extension.models.ExtendedAccount"):
                migrate()
            assert {path.name for path in scripts.glob("*.py")} == {"initial.py"}
            assert _tables(db_path) == ({"alembic_version"} if operation == "revision" else set())
            if operation == "revision":
                assert _versions(db_path) == {"ow_core0"}
            return

        migrate()
        if operation == "revision":
            manager.upgrade(core)
        with sqlite3.connect(db_path) as connection:
            assert [row[1] for row in connection.execute("PRAGMA table_info(account)")] == ["id", "extension_value"]

        registry.install(extension)
        with pytest.raises(MigrationError, match="multiple apps"):
            manager.revision(core, "explicit owner cannot override a conflict")


@pytest.fixture
def schema_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[sa.Engine]:
    """Use one real engine with a persistent attached SQLite schema."""
    engine = sa.create_engine(_database_url(tmp_path / "schemas.db"))

    @sa.event.listens_for(engine, "connect")
    def attach(connection: sqlite3.Connection, _record: object) -> None:
        connection.execute("ATTACH DATABASE ? AS archive", (str(tmp_path / "archive.db"),))

    monkeypatch.setattr(sa, "create_engine", lambda *args, **kwargs: engine)
    monkeypatch.setattr(sa, "engine_from_config", lambda *args, **kwargs: engine)
    try:
        yield engine
    finally:
        engine.dispose()


class TestAutogenerateIsolation:
    """Execute generated migrations to verify table ownership, not script formatting."""

    def test_schema_qualified_names_stay_isolated(
        self, module_factory: ModuleFactory, schema_engine: sa.Engine
    ) -> None:
        core = _migration_app(
            module_factory, "ag_core", _model_source("Account", "account", "    name = Column(String)\n")
        )
        plugin = _migration_app(
            module_factory,
            "ag_plugin",
            _model_source("ArchivedAccount", "account", "    __table_args__ = {'schema': 'archive'}\n"),
        )
        registry = ModuleRegistry()
        registry.populate([core, plugin])
        manager = MigrationsManager(registry, str(schema_engine.url))
        command.stamp(manager._build_config(None), "heads")
        with schema_engine.begin() as connection:
            connection.execute(sa.text("CREATE TABLE account (id INTEGER NOT NULL PRIMARY KEY)"))
            connection.execute(sa.text("INSERT INTO account (id) VALUES (42)"))
        manager.revision(plugin, "create archive account")
        manager.upgrade(plugin)
        with schema_engine.connect() as connection:
            assert sa.inspect(connection).get_table_names(schema="archive") == ["account"]
            assert connection.execute(sa.text("SELECT id FROM account")).all() == [(42,)]
            assert [column["name"] for column in sa.inspect(connection).get_columns("account")] == ["id"]
        manager.downgrade(plugin, "-1")
        with schema_engine.connect() as connection:
            assert sa.inspect(connection).get_table_names(schema="archive") == []
            assert connection.execute(sa.text("SELECT id FROM account")).all() == [(42,)]

    @pytest.mark.parametrize("target", ["ag_core", "ag_plugin"])
    @pytest.mark.parametrize("foreign_exists", [False, True])
    def test_revision_only_changes_target_tables(
        self, module_factory: ModuleFactory, tmp_path: Path, target: str, foreign_exists: bool
    ) -> None:
        core = _migration_app(module_factory, "ag_core", _model_source("Account", "account"))
        plugin = _migration_app(module_factory, "ag_plugin", _model_source("Tag", "tag"))
        registry = ModuleRegistry()
        registry.populate([core, plugin])
        db_path = tmp_path / "generate.db"
        manager = MigrationsManager(registry, _database_url(db_path))
        command.stamp(manager._build_config(None), "heads")
        own, foreign = ("account", "tag") if target == core else ("tag", "account")
        if foreign_exists:
            with sqlite3.connect(db_path) as connection:
                connection.execute(f"CREATE TABLE {foreign} (id INTEGER NOT NULL PRIMARY KEY)")
                connection.execute(f"INSERT INTO {foreign} VALUES (42)")
        manager.revision(target, "create owned table")
        manager.upgrade(target)
        expected = {"alembic_version", foreign} if foreign_exists else {"alembic_version"}
        assert _tables(db_path) == expected | {own}
        manager.downgrade(target, "-1")
        assert _tables(db_path) == expected
        assert _versions(db_path) == {"ag_core0", "ag_plugin0"}
        if foreign_exists:
            assert _sqlite_rows(db_path, f"SELECT id FROM {foreign}") == {42}

    def test_imported_model_change_stays_outside_revision(self, module_factory: ModuleFactory, tmp_path: Path) -> None:
        core = _migration_app(
            module_factory, "ag_core", _model_source("Account", "account", "    name = Column(String)\n")
        )
        plugin = _migration_app(
            module_factory,
            "ag_plugin",
            _model_source("Tag", "tag", "    name = Column(String(30), index=True)\n")
            + "from ag_core.models import Account\n",
        )
        registry = ModuleRegistry()
        registry.populate([core, plugin])
        db_path = tmp_path / "columns.db"
        manager = MigrationsManager(registry, _database_url(db_path))
        command.stamp(manager._build_config(None), "heads")
        with sqlite3.connect(db_path) as connection:
            connection.execute("CREATE TABLE account (id INTEGER NOT NULL PRIMARY KEY)")
            connection.execute("CREATE TABLE tag (id INTEGER NOT NULL PRIMARY KEY)")
        manager.revision(plugin, "add tag name")
        manager.upgrade(plugin)
        with sqlite3.connect(db_path) as connection:
            assert [row[1] for row in connection.execute("PRAGMA table_info(tag)")] == ["id", "name"]
            assert [row[1] for row in connection.execute("PRAGMA table_info(account)")] == ["id"]
            assert [row[1] for row in connection.execute("PRAGMA index_list(tag)")] == ["ix_tag_name"]
        manager.downgrade(plugin, "-1")
        with sqlite3.connect(db_path) as connection:
            assert [row[1] for row in connection.execute("PRAGMA table_info(tag)")] == ["id"]
            assert [row[1] for row in connection.execute("PRAGMA table_info(account)")] == ["id"]

    def test_association_foreign_key_is_retained(self, module_factory: ModuleFactory, tmp_path: Path) -> None:
        core = _migration_app(module_factory, "ag_core", _model_source("Account", "account"))
        plugin = _migration_app(
            module_factory,
            "ag_plugin",
            "from sqlalchemy import Table, Column, Integer, ForeignKey\n"
            "from bedrock.database.base import BedrockModel\n"
            "links = Table('links', BedrockModel.metadata,\n"
            "    Column('user_id', Integer, ForeignKey('account.id'), primary_key=True),\n"
            "    info={'bedrock_module': 'ag_plugin'})\n",
        )
        registry = ModuleRegistry()
        registry.populate([core, plugin])
        db_path = tmp_path / "foreign_key.db"
        manager = MigrationsManager(registry, _database_url(db_path))
        manager.ensure_schema(core)
        command.stamp(manager._build_config(None), "heads")
        manager.revision(plugin, "add account links")
        manager.upgrade(plugin)
        with sqlite3.connect(db_path) as connection:
            assert [(row[2], row[3], row[4]) for row in connection.execute("PRAGMA foreign_key_list(links)")] == [
                ("account", "user_id", "id")
            ]
        manager.downgrade(plugin, "-1")
        assert _tables(db_path) == {"account", "alembic_version"}

    @pytest.mark.parametrize("case", ["missing", "conflict", "duplicate_schema"])
    def test_invalid_ownership_writes_no_revision(
        self, module_factory: ModuleFactory, tmp_path: Path, case: str
    ) -> None:
        models = _model_source("Invalid", "invalid")
        if case == "missing":
            models += "unknown = Table('unknown', BedrockModel.metadata, Column('id', Integer))\n"
        elif case == "conflict":
            models += "Invalid.__table__.info['bedrock_module'] = 'ag_other'\n"
        else:
            models += (
                "alias = Table('invalid', BedrockModel.metadata, Column('id', Integer),\n"
                "    schema='main', info={'bedrock_module': 'ag_invalid'})\n"
            )
        app = _migration_app(module_factory, "ag_invalid", models)
        other = _migration_app(module_factory, "ag_other", "")
        registry = ModuleRegistry()
        registry.populate([app, other])
        db_path = tmp_path / "invalid_generation.db"
        manager = MigrationsManager(registry, _database_url(db_path))
        command.stamp(manager._build_config(None), "heads")
        with pytest.raises(MigrationError):
            manager.revision(app, "must not be written")
        assert {p.name for p in (module_factory.root / app / "migrations").glob("*.py")} == {"initial.py"}
        assert _versions(db_path) == {"ag_invalid0", "ag_other0"}
        assert _tables(db_path) == {"alembic_version"}

    def test_missing_autogenerate_scope_is_rejected(self, module_factory: ModuleFactory, tmp_path: Path) -> None:
        app = _migration_app(module_factory, "ag_app", _model_source("Account", "account"))
        registry = ModuleRegistry()
        registry.populate([app])
        manager = MigrationsManager(registry, _database_url(tmp_path / "missing_scope.db"))
        cfg = manager._build_config(app)
        command.stamp(cfg, "heads")
        with pytest.raises(MigrationError):
            command.revision(cfg, message="no scope", autogenerate=True, head=f"{app}@head")
        assert {p.name for p in (module_factory.root / app / "migrations").glob("*.py")} == {"initial.py"}

    def test_empty_module_generates_no_foreign_operations(self, module_factory: ModuleFactory, tmp_path: Path) -> None:
        core = _migration_app(module_factory, "ag_core", _model_source("Account", "account"))
        empty = _migration_app(module_factory, "ag_empty", "")
        registry = ModuleRegistry()
        registry.populate([core, empty])
        db_path = tmp_path / "empty_generation.db"
        manager = MigrationsManager(registry, _database_url(db_path))
        command.stamp(manager._build_config(None), "heads")
        manager.revision(empty, "no owned changes")
        manager.upgrade(empty)
        manager.uninstall(empty)
        assert _tables(db_path) == {"alembic_version"}
        assert _versions(db_path) == {"ag_core0"}

    @pytest.mark.parametrize("schema", [None, "main"])
    def test_default_schema_aliases_do_not_create_phantom_tables(
        self, module_factory: ModuleFactory, tmp_path: Path, schema: str | None
    ) -> None:
        app = _migration_app(
            module_factory,
            "ag_app",
            _model_source("Account", "account", f"    __table_args__ = {{'schema': {schema!r}}}\n"),
        )
        registry = ModuleRegistry()
        registry.populate([app])
        db_path = tmp_path / "default_schema.db"
        manager = MigrationsManager(registry, _database_url(db_path))
        manager.ensure_schema(app)
        with sqlite3.connect(db_path) as connection:
            connection.execute("INSERT INTO account VALUES (42)")
        manager.revision(app, "no changes")
        manager.upgrade(app)
        manager.downgrade(app, "-1")
        assert _sqlite_rows(db_path, "SELECT id FROM account") == {42}

    def test_install_uninstall_preserves_foreign_data_and_revision(
        self, module_factory: ModuleFactory, tmp_path: Path
    ) -> None:
        core = _migration_app(module_factory, "ag_core", _model_source("Account", "account"))
        plugin = module_factory("ag_plugin", models=_model_source("Tag", "tag"), files={"migrations/__init__.py": ""})
        registry = ModuleRegistry()
        registry.populate([core, plugin])
        db_path = tmp_path / "lifecycle.db"
        manager = MigrationsManager(registry, _database_url(db_path))
        manager.ensure_schema(core)
        with sqlite3.connect(db_path) as connection:
            connection.execute("INSERT INTO account VALUES (42)")
        manager.revision(plugin, "initial plugin")
        assert manager.ensure_schema(plugin) == "created"
        assert _tables(db_path) == {"account", "tag", "alembic_version"}
        manager.uninstall(plugin)
        assert _tables(db_path) == {"account", "alembic_version"}
        assert _sqlite_rows(db_path, "SELECT id FROM account") == {42}
        assert _versions(db_path) == {"ag_core0"}

    def test_removed_model_requires_explicit_drop(self, module_factory: ModuleFactory, tmp_path: Path) -> None:
        app = _migration_app(module_factory, "ag_app", "")
        registry = ModuleRegistry()
        registry.populate([app])
        db_path = tmp_path / "historical.db"
        manager = MigrationsManager(registry, _database_url(db_path))
        command.stamp(manager._build_config(None), "heads")
        with sqlite3.connect(db_path) as connection:
            connection.execute("CREATE TABLE removed (id INTEGER NOT NULL PRIMARY KEY)")
            connection.execute("INSERT INTO removed VALUES (42)")
        manager.revision(app, "no inferred drop")
        manager.upgrade(app)
        assert _sqlite_rows(db_path, "SELECT id FROM removed") == {42}
        revision = manager.get_history(app)[0]["revision"]
        (module_factory.root / app / "migrations" / "explicit.py").write_text(
            "from alembic import op\nimport sqlalchemy as sa\n"
            "revision = 'explicit_drop'\n"
            f"down_revision = {revision!r}\nbranch_labels = None\ndepends_on = None\n"
            "def upgrade():\n    op.drop_table('removed')\n"
            "def downgrade():\n"
            "    op.create_table('removed', sa.Column('id', sa.Integer, primary_key=True))\n",
            encoding="utf-8",
        )
        manager.upgrade(app)
        assert "removed" not in _tables(db_path)
        manager.downgrade(app, "-1")
        assert "removed" in _tables(db_path)
