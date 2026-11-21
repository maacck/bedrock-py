"""Tests for module-scoped migrations: locks, targets, cross-app safety, ownership, and generation."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
import sqlalchemy as sa
from bedrock.cli.db import db_app
from bedrock.database import BranchRevisionStatus, MigrationError, MigrationsManager
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

        monkeypatch.setattr("bedrock.database.migration.manager.create_engine", fail_create)
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

    def test_upgrade_and_downgrade_hold_the_lock(
        self, module_factory: ModuleFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Scripts run only while the migration lock is held."""

        log = tmp_path / "events.log"
        monkeypatch.setenv("MIGRATION_LOG", str(log))

        @contextmanager
        def lock(self: MigrationsManager) -> Iterator[None]:
            _log_event("enter")
            yield
            _log_event("exit")

        monkeypatch.setattr(MigrationsManager, "_migration_lock", lock)
        app = _chain_app(module_factory, "lk_app", {"r1": _logging_script("r1")})
        registry = ModuleRegistry()
        registry.populate([app])
        manager = MigrationsManager(registry, _database_url(tmp_path / "lock.db"))

        manager.upgrade(app)
        manager.downgrade(app, "base")

        assert log.read_text().split() == ["enter", "upgrade", "exit", "enter", "downgrade", "exit"]


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
            assert "bedrock_migrations" not in tables


def _query(db_path: Path, sql: str) -> list[tuple[object, ...]]:
    """Return every row of *sql* run on a fresh connection to *db_path*."""

    with closing(sqlite3.connect(db_path)) as connection:
        return [tuple(row) for row in connection.execute(sql)]


def _sqlite_rows(db_path: Path, query: str) -> set[str]:
    """Return the first column of *query* run against *db_path*."""

    return {row[0] for row in _query(db_path, query)}


def _versions(db_path: Path) -> set[tuple[str, str]]:
    """Return the executed ``(app, revision)`` keys."""

    return {
        (str(app), str(revision)) for app, revision in _query(db_path, "SELECT app, revision FROM bedrock_migrations")
    }


def _keys(app: str, *revisions: str) -> set[tuple[str, str]]:
    return {(app, revision) for revision in revisions}


def _tables(db_path: Path) -> set[str]:
    return _sqlite_rows(db_path, "SELECT name FROM sqlite_master WHERE type = 'table'")


def _snapshot(db_path: Path) -> tuple[set[str], list[tuple[object, ...]]]:
    """Capture every table name and version row so a refused command can prove it wrote nothing."""

    rows = _query(db_path, "SELECT app, revision, parents, origin FROM bedrock_migrations ORDER BY app, revision")
    return _tables(db_path), rows


def _execute(db_path: Path, sql: str) -> None:
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute(sql)
        connection.commit()


def _log_event(event: str) -> None:
    import os

    with open(os.environ["MIGRATION_LOG"], "a", encoding="utf-8") as log:
        log.write(event + "\n")


def _script(
    rev: str,
    down: str | None,
    depends_on: tuple[str, ...] = (),
    *,
    table: str | None = None,
    fail: str | None = None,
) -> str:
    """A revision that creates (and drops) *table*, optionally raising after the DDL of one direction."""

    table = table or rev
    boom = "    raise RuntimeError('boom')\n"
    return (
        "from alembic import op\nimport sqlalchemy as sa\n"
        f"revision = {rev!r}\ndown_revision = {down!r}\ndepends_on = {depends_on!r}\n"
        f"def upgrade():\n    op.create_table({table!r}, sa.Column('id', sa.Integer, primary_key=True))\n"
        f"{boom if fail == 'upgrade' else ''}"
        f"def downgrade():\n    op.drop_table({table!r})\n"
        f"{boom if fail == 'downgrade' else ''}"
    )


def _logging_script(rev: str) -> str:
    """A DDL-free revision that records which direction ran."""

    return (
        "import os\n"
        f"revision = {rev!r}\ndown_revision = None\ndepends_on = ()\n"
        "def _log(event):\n    with open(os.environ['MIGRATION_LOG'], 'a') as log:\n        log.write(event + '\\n')\n"
        "def upgrade():\n    _log('upgrade')\n"
        "def downgrade():\n    _log('downgrade')\n"
    )


def _chain_app(factory: ModuleFactory, name: str, scripts: Mapping[str, str], depends_on: tuple[str, ...] = ()) -> str:
    """Create a module whose migrations are *scripts* (revision to source)."""

    return factory(
        name,
        manifest={"title": name, "version": "0.1.0", "depends_on": list(depends_on)},
        models="",
        files={f"migrations/{rev}.py": src for rev, src in scripts.items()},
    )


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

        assert manager.downgrade("sec02_a", "base") == [("sec02_a", "sec02ab"), ("sec02_a", "sec02aa")]

        assert _versions(db_path) == _keys("sec02_b", "sec02ba", "sec02bb")
        assert "sec02_b" in _tables(db_path)
        assert "sec02_a" not in _tables(db_path)

    def test_relative_downgrade_moves_only_the_app_branch(self, two_apps: tuple[MigrationsManager, Path]) -> None:
        manager, db_path = two_apps

        manager.downgrade("sec02_a", "-1")

        assert _versions(db_path) == _keys("sec02_a", "sec02aa") | _keys("sec02_b", "sec02ba", "sec02bb")

    def test_relative_upgrade_runs_only_the_requested_count(self, two_apps: tuple[MigrationsManager, Path]) -> None:
        manager, db_path = two_apps
        manager.downgrade("sec02_a", "base")

        assert manager.upgrade("sec02_a", "+1") == [("sec02_a", "sec02aa")]

        assert _versions(db_path) == _keys("sec02_a", "sec02aa") | _keys("sec02_b", "sec02ba", "sec02bb")

    @pytest.mark.parametrize(
        ("method", "target"),
        [
            ("downgrade", "sec02ba"),
            ("downgrade", "sec02_b@base"),
            ("upgrade", "heads"),
            ("upgrade", "sec02bb"),
            ("downgrade", "sec02_a@sec02ba"),
            ("upgrade", "sec02_a@sec02bb"),
            ("upgrade", "+1"),
            ("downgrade", "-3"),
        ],
    )
    def test_targets_outside_the_app_branch_are_rejected(
        self, two_apps: tuple[MigrationsManager, Path], method: str, target: str
    ) -> None:
        manager, db_path = two_apps

        with pytest.raises(MigrationError):
            getattr(manager, method)("sec02_a", target)

        assert _versions(db_path) == _keys("sec02_a", "sec02aa", "sec02ab") | _keys("sec02_b", "sec02ba", "sec02bb")


def test_percent_encoded_database_url_is_used_verbatim(populate: Callable[..., ModuleRegistry], tmp_path: Path) -> None:
    """URL-encoded characters (as in an encoded password) reach the engine unchanged."""

    registry = populate(("sec03_app", "sec03"))
    db_path = tmp_path / "encoded.db"

    MigrationsManager(registry, f"{_database_url(db_path)}?timeout=%32%30").upgrade("sec03_app")

    assert _versions(db_path) == _keys("sec03_app", "sec03a", "sec03b")


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


@dataclass
class _World:
    """Three cross-dependent apps over one SQLite database."""

    factory: ModuleFactory
    db_path: Path
    auth: str
    orders: str
    billing: str

    def manager(self, *roots: str) -> MigrationsManager:
        """A manager whose registry loads only *roots* and their manifest dependencies."""

        registry = ModuleRegistry()
        registry.populate(list(roots))
        return MigrationsManager(registry, _database_url(self.db_path))

    def write(self, app: str, rev: str, down: str | None, depends_on: tuple[str, ...] = (), **options: str) -> None:
        """Write (or overwrite) one revision script of *app*."""

        path = self.factory.root / app / "migrations" / f"{rev}.py"
        path.write_text(_script(rev, down, depends_on, **options), encoding="utf-8")

    def migrate(self, *apps: str) -> MigrationsManager:
        """Upgrade *apps* (default: all three) with everything loaded."""

        manager = self.manager(self.auth, self.orders, self.billing)
        for app in apps or (self.auth, self.orders, self.billing):
            manager.upgrade(app)
        return manager


@pytest.fixture
def world(module_factory: ModuleFactory, tmp_path: Path) -> _World:
    """auth (a1 -> a2); orders (o1) and billing (b1) each depend on auth:a2."""

    auth = _chain_app(module_factory, "ms_auth", {"a1": _script("a1", None), "a2": _script("a2", "a1")})
    dependency = (f"{auth}:a2",)
    orders = _chain_app(module_factory, "ms_orders", {"o1": _script("o1", None, dependency)}, depends_on=(auth,))
    billing = _chain_app(module_factory, "ms_billing", {"b1": _script("b1", None, dependency)}, depends_on=(auth,))
    return _World(module_factory, tmp_path / "world.db", auth, orders, billing)


class TestModuleScopedMigrations:
    """Apps share one version table but only the requested app (and its prerequisites) ever moves."""

    def test_unrelated_apps_with_same_revision_name_are_independent(
        self, module_factory: ModuleFactory, tmp_path: Path
    ) -> None:
        left = _chain_app(module_factory, "ind_left", {"0001_initial": _script("0001_initial", None, table="left_t")})
        right = _chain_app(
            module_factory, "ind_right", {"0001_initial": _script("0001_initial", None, table="right_t")}
        )
        registry = ModuleRegistry()
        registry.populate([left, right])
        db_path = tmp_path / "independent.db"
        manager = MigrationsManager(registry, _database_url(db_path))
        manager.upgrade(left)
        manager.upgrade(right)
        _execute(db_path, "INSERT INTO right_t VALUES (7)")
        before_right = _query(db_path, "SELECT * FROM bedrock_migrations WHERE app = 'ind_right'")

        assert manager.downgrade(left, "base") == [(left, "0001_initial")]

        assert _versions(db_path) == _keys(right, "0001_initial")
        assert _tables(db_path) == {"right_t", "bedrock_migrations"}
        assert _sqlite_rows(db_path, "SELECT id FROM right_t") == {7}
        assert _query(db_path, "SELECT * FROM bedrock_migrations") == before_right

    def test_partial_registry_upgrade_keeps_unloaded_rows(self, world: _World) -> None:
        world.migrate()
        world.write(world.orders, "o2", "o1", (f"{world.auth}:a2",))

        assert world.manager(world.orders).upgrade(world.orders) == [(world.orders, "o2")]

        assert _versions(world.db_path) == (
            _keys(world.auth, "a1", "a2") | _keys(world.orders, "o1", "o2") | _keys(world.billing, "b1")
        )
        assert {"o2", "b1"} <= _tables(world.db_path)

    def test_upgrade_runs_only_the_prerequisites_it_needs(self, world: _World) -> None:
        world.write(world.auth, "a3", "a2")
        world.manager(world.auth).upgrade(world.auth, "a1")

        assert world.manager(world.orders).upgrade(world.orders) == [(world.auth, "a2"), (world.orders, "o1")]

        assert _versions(world.db_path) == _keys(world.auth, "a1", "a2") | _keys(world.orders, "o1")
        assert "a3" not in _tables(world.db_path)

    def test_rollback_is_blocked_by_dependents_unless_cascade(self, world: _World) -> None:
        manager = world.manager(world.orders)
        manager.upgrade(world.auth)
        manager.upgrade(world.orders)
        before = _snapshot(world.db_path)

        with pytest.raises(MigrationError, match=f"{world.orders}:o1"):
            manager.downgrade(world.auth, "a1")
        assert _snapshot(world.db_path) == before

        assert manager.downgrade(world.auth, "a1", cascade=True) == [(world.orders, "o1"), (world.auth, "a2")]

        assert _versions(world.db_path) == _keys(world.auth, "a1")
        assert _tables(world.db_path) == {"a1", "bedrock_migrations"}

    @pytest.mark.parametrize("cascade", [False, True])
    def test_rollback_with_unloaded_dependent_is_always_refused(self, world: _World, cascade: bool) -> None:
        world.migrate()
        before = _snapshot(world.db_path)

        with pytest.raises(MigrationError, match=f"{world.billing}:b1"):
            world.manager(world.orders).downgrade(world.auth, "a1", cascade=cascade)

        assert _snapshot(world.db_path) == before

    def test_cascade_follows_dependents_transitively(self, module_factory: ModuleFactory, tmp_path: Path) -> None:
        base = _chain_app(module_factory, "tr_base", {"t1": _script("t1", None)})
        mid = _chain_app(module_factory, "tr_mid", {"m1": _script("m1", None, (f"{base}:t1",))}, depends_on=(base,))
        top = _chain_app(module_factory, "tr_top", {"p1": _script("p1", None, (f"{mid}:m1",))}, depends_on=(mid,))
        db_path = tmp_path / "transitive.db"

        def manager(*roots: str) -> MigrationsManager:
            registry = ModuleRegistry()
            registry.populate(list(roots))
            return MigrationsManager(registry, _database_url(db_path))

        everything = manager(base, mid, top)
        for app in (base, mid, top):
            everything.upgrade(app)
        before = _snapshot(db_path)

        with pytest.raises(MigrationError, match=f"{mid}:m1"):
            everything.downgrade(base, "base")
        with pytest.raises(MigrationError, match=f"{top}:p1"):
            manager(mid).downgrade(base, "base", cascade=True)
        assert _snapshot(db_path) == before

        assert everything.downgrade(base, "base", cascade=True) == [(top, "p1"), (mid, "m1"), (base, "t1")]
        assert _tables(db_path) == {"bedrock_migrations"}
        assert _versions(db_path) == set()

    def test_missing_script_of_an_executed_migration_is_an_error(self, world: _World) -> None:
        world.migrate()
        (world.factory.root / world.orders / "migrations" / "o1.py").unlink()
        before = _snapshot(world.db_path)

        with pytest.raises(MigrationError, match="o1"):
            world.manager(world.orders).upgrade(world.orders)

        assert _snapshot(world.db_path) == before

    @pytest.mark.parametrize(
        ("executed", "rewritten"),
        [(("ms_auth:a2",), ()), ((), ("ms_auth:a2",))],
        ids=["dependency-removed", "dependency-added"],
    )
    def test_changed_dependencies_of_executed_migration_are_rejected(
        self, world: _World, executed: tuple[str, ...], rewritten: tuple[str, ...]
    ) -> None:
        world.write(world.orders, "o1", None, executed)
        world.migrate(world.auth, world.orders)
        world.write(world.orders, "o1", None, rewritten)
        world.write(world.orders, "o2", "o1", (f"{world.auth}:a2",))
        before = _snapshot(world.db_path)
        manager = world.manager(world.orders)

        with pytest.raises(MigrationError, match="o1"):
            manager.upgrade(world.orders)
        with pytest.raises(MigrationError, match="o1"):
            manager.downgrade(world.orders, "base")

        assert _snapshot(world.db_path) == before

    def test_removed_local_parent_blocks_cascade_before_first_step(self, world: _World) -> None:
        world.write(world.orders, "o2", "o1")
        world.migrate(world.auth, world.orders)
        world.write(world.orders, "o2", None)
        manager = world.manager(world.orders)
        before = _snapshot(world.db_path)
        with pytest.raises(MigrationError, match="Dependency drift.*o2"):
            manager.downgrade(world.auth, "base", cascade=True)
        assert _snapshot(world.db_path) == before


class TestBaselineProtection:
    """Baseline rows record imported positions, not scripts that ran, so nothing may undo them."""

    @pytest.fixture
    def baselined(self, world: _World) -> _World:
        world.migrate(world.auth, world.orders)
        _execute(world.db_path, "INSERT INTO a1 VALUES (7)")
        return world

    def _mark(self, world: _World, app: str, rev: str) -> None:
        _execute(
            world.db_path,
            f"UPDATE bedrock_migrations SET origin = 'baseline' WHERE app = '{app}' AND revision = '{rev}'",
        )

    @pytest.mark.parametrize("operation", ["downgrade", "uninstall"])
    def test_rollback_through_a_baseline_writes_nothing(self, baselined: _World, operation: str) -> None:
        self._mark(baselined, baselined.auth, "a1")
        before = _snapshot(baselined.db_path)
        manager = baselined.manager(baselined.orders)

        with pytest.raises(MigrationError, match="(?i)baseline"):
            if operation == "downgrade":
                manager.downgrade(baselined.auth, "base", cascade=True)
            else:
                manager.uninstall(baselined.auth, cascade=True)

        assert _snapshot(baselined.db_path) == before
        assert _sqlite_rows(baselined.db_path, "SELECT id FROM a1") == {7}

    def test_cascade_reaching_a_baseline_writes_nothing(self, baselined: _World) -> None:
        self._mark(baselined, baselined.orders, "o1")
        before = _snapshot(baselined.db_path)

        with pytest.raises(MigrationError, match="(?i)baseline"):
            baselined.manager(baselined.orders).downgrade(baselined.auth, "a1", cascade=True)

        assert _snapshot(baselined.db_path) == before

    def test_rolling_back_to_the_baseline_itself_is_allowed(self, baselined: _World) -> None:
        self._mark(baselined, baselined.auth, "a1")

        steps = baselined.manager(baselined.orders).downgrade(baselined.auth, "a1", cascade=True)

        assert steps == [(baselined.orders, "o1"), (baselined.auth, "a2")]
        assert _versions(baselined.db_path) == _keys(baselined.auth, "a1")
        assert _sqlite_rows(baselined.db_path, "SELECT id FROM a1") == {7}

    def test_apps_outside_the_baseline_closure_still_roll_back(self, baselined: _World) -> None:
        self._mark(baselined, baselined.auth, "a1")

        assert baselined.manager(baselined.orders).downgrade(baselined.orders, "base") == [(baselined.orders, "o1")]

        assert _versions(baselined.db_path) == _keys(baselined.auth, "a1", "a2")


class TestMigrationFailures:
    """A failing script leaves neither its DDL nor its version row behind on SQLite."""

    @pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
    def test_failed_script_rolls_back_ddl_and_version_row(
        self, module_factory: ModuleFactory, tmp_path: Path, direction: str
    ) -> None:
        app = _chain_app(module_factory, "rb_app", {"r1": _script("r1", None), "r2": _script("r2", "r1")})
        registry = ModuleRegistry()
        registry.populate([app])
        db_path = tmp_path / "rollback.db"
        manager = MigrationsManager(registry, _database_url(db_path))
        script = module_factory.root / app / "migrations" / "r2.py"
        if direction == "upgrade":
            script.write_text(_script("r2", "r1", fail="upgrade"), encoding="utf-8")
            with pytest.raises((MigrationError, RuntimeError)):
                manager.upgrade(app)
            assert _versions(db_path) == _keys(app, "r1")
            assert _tables(db_path) == {"r1", "bedrock_migrations"}
        else:
            manager.upgrade(app)
            script.write_text(_script("r2", "r1", fail="downgrade"), encoding="utf-8")
            with pytest.raises((MigrationError, RuntimeError)):
                manager.downgrade(app, "-1")
            assert _versions(db_path) == _keys(app, "r1", "r2")
            assert _tables(db_path) == {"r1", "r2", "bedrock_migrations"}


class TestEnsureSchema:
    """Installation runs the module's migrations; it never fabricates schema."""

    def test_ensure_schema_requires_scripts(self, module_factory: ModuleFactory, tmp_path: Path) -> None:
        app = _model_app(module_factory, "es_models", _model_source("Account", "account"))
        registry = ModuleRegistry()
        registry.populate([app])
        db_path = tmp_path / "no_scripts.db"

        with pytest.raises(MigrationError, match="migration scripts"):
            MigrationsManager(registry, _database_url(db_path)).ensure_schema(app)

        assert _tables(db_path) == set()

    def test_ensure_schema_statuses(self, module_factory: ModuleFactory, tmp_path: Path) -> None:
        app = _chain_app(module_factory, "es_chain", {"r1": _script("r1", None)})
        registry = ModuleRegistry()
        registry.populate([app])
        db_path = tmp_path / "statuses.db"
        manager = MigrationsManager(registry, _database_url(db_path))

        assert manager.ensure_schema(app) == "created"
        assert manager.ensure_schema(app) == "up-to-date"
        (module_factory.root / app / "migrations" / "r2.py").write_text(_script("r2", "r1"), encoding="utf-8")
        assert manager.ensure_schema(app) == "upgraded"

        assert _tables(db_path) == {"r1", "r2", "bedrock_migrations"}

    @pytest.mark.parametrize("method", ["upgrade", "downgrade", "ensure_schema", "uninstall"])
    def test_legacy_alembic_state_requires_adopt(
        self, populate: Callable[..., ModuleRegistry], tmp_path: Path, method: str
    ) -> None:
        registry = populate(("lg_app", "lg"))
        db_path = tmp_path / "legacy.db"
        _execute(db_path, "CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
        _execute(db_path, "INSERT INTO alembic_version VALUES ('lgb')")
        manager = MigrationsManager(registry, _database_url(db_path))
        args = ("lg_app", "base") if method == "downgrade" else ("lg_app",)

        with pytest.raises(MigrationError, match="adopt"):
            getattr(manager, method)(*args)

        assert _tables(db_path) == {"alembic_version"}


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


def _model_app(
    factory: ModuleFactory,
    name: str,
    models: str,
    files: dict[str, str] | None = None,
    manifest: dict[str, object] | None = None,
) -> str:
    """Create a model-owning app that has no migration scripts yet."""
    return factory(name, models=models, files=files, manifest=manifest)


def _migration_app(factory: ModuleFactory, name: str, models: str, files: dict[str, str] | None = None) -> str:
    """Create a model-owning app whose only revision is an empty script in the legacy format (with branch_labels)."""
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


def _install(manager: MigrationsManager, *apps: str) -> None:
    """Generate each app's initial migration from its models, then run it."""
    for app in apps:
        manager.revision(app, "initial schema")
        manager.ensure_schema(app)


def _head_keys(manager: MigrationsManager, *apps: str) -> set[tuple[str, str]]:
    return {(app, manager.heads(app)[0]) for app in apps}


def _baseline(manager: MigrationsManager, *apps: str) -> None:
    """Execute each app's empty initial revision so later revisions start from head."""
    for app in apps:
        manager.upgrade(app)


def test_disjoint_registries_on_one_database(module_factory: ModuleFactory, tmp_path: Path) -> None:
    core = _model_app(module_factory, "scope_core", _model_source("Account", "accounts"))
    plugin = _model_app(module_factory, "scope_plugin", _model_source("Tag", "tags"))
    core_registry, plugin_registry = ModuleRegistry(), ModuleRegistry()
    core_registry.populate([core])
    plugin_registry.populate([plugin])
    db_path = tmp_path / "shared.db"
    core_mgr = MigrationsManager(core_registry, _database_url(db_path))
    plugin_mgr = MigrationsManager(plugin_registry, _database_url(db_path), version_table="plugin_migrations")
    core_mgr.revision(core, "initial")
    plugin_mgr.revision(plugin, "initial")
    assert core_mgr.ensure_schema(core) == "created"
    assert _tables(db_path) == {"accounts", "bedrock_migrations"}
    assert plugin_mgr.current(plugin) == ()
    assert plugin_mgr.upgrade(plugin) == [(plugin, "0001_initial")]
    assert _tables(db_path) == {"accounts", "tags", "bedrock_migrations", "plugin_migrations"}
    assert plugin_mgr.uninstall(plugin) == [(plugin, "0001_initial")]
    assert core_mgr.current(core) == ("0001_initial",)
    assert _tables(db_path) == {"accounts", "bedrock_migrations", "plugin_migrations"}
    assert _versions(db_path) == {(core, "0001_initial")}
    assert not _sqlite_rows(db_path, "SELECT revision FROM plugin_migrations")


@pytest.mark.parametrize("reserved", ["plugin_versions", "plugin_versions_adopt"])
def test_autogenerate_rejects_custom_ledger_model(module_factory: ModuleFactory, tmp_path: Path, reserved: str) -> None:
    plugin = _model_app(module_factory, "bad_plugin", _model_source("Bad", reserved))
    registry = ModuleRegistry()
    registry.populate([plugin])
    manager = MigrationsManager(registry, _database_url(tmp_path / "bad.db"), version_table="plugin_versions")
    with pytest.raises(MigrationError, match="reserved"):
        manager.revision(plugin, "initial")
    assert not (module_factory.root / plugin / "migrations").exists()


class TestTableOwnership:
    """Generated initial migrations create only the tables actually owned by their app."""

    def test_first_install_includes_declared_association_table(
        self, module_factory: ModuleFactory, tmp_path: Path
    ) -> None:
        core = _model_app(module_factory, "ow_core", _model_source("Account", "account"))
        plugin = _model_app(
            module_factory,
            "ow_plugin",
            "from sqlalchemy import Table, Column, Integer, ForeignKey\n"
            "from bedrock.database.base import BedrockModel\n"
            "links = Table('links', BedrockModel.metadata,\n"
            "    Column('user_id', Integer, ForeignKey('account.id'), primary_key=True),\n"
            "    info={'bedrock_module': 'ow_plugin'})\n",
        )
        other = _model_app(module_factory, "ow_other", _model_source("Other", "other"))
        registry = ModuleRegistry()
        registry.populate([core, plugin, other])
        db_path = tmp_path / "owned.db"
        manager = MigrationsManager(registry, _database_url(db_path))
        _install(manager, core, plugin)

        assert _tables(db_path) == {"account", "links", "bedrock_migrations"}
        assert _versions(db_path) == _head_keys(manager, core, plugin)

    def test_first_install_uses_definition_owner(self, module_factory: ModuleFactory, tmp_path: Path) -> None:
        core = _model_app(module_factory, "ow_core", _model_source("Account", "account"))
        plugin = _model_app(
            module_factory,
            "ow_plugin",
            _model_source("Tag", "tag") + "from ow_core.models import Account as ImportedAccount\n",
        )
        registry = ModuleRegistry()
        registry.populate([core, plugin])
        db_path = tmp_path / "imported.db"
        manager = MigrationsManager(registry, _database_url(db_path))
        _install(manager, plugin)

        assert _tables(db_path) == {"tag", "bedrock_migrations"}
        assert _versions(db_path) == _head_keys(manager, plugin)

    def test_first_install_includes_loaded_domain_models(self, module_factory: ModuleFactory, tmp_path: Path) -> None:
        parent = _model_app(
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
        neighbor = _model_app(module_factory, "ow_application", _model_source("Neighbor", "neighbor"))
        registry = ModuleRegistry()
        registry.populate([parent, "ow_app.child", neighbor])
        db_path = tmp_path / "domains.db"
        _install(MigrationsManager(registry, _database_url(db_path)), parent)

        assert _tables(db_path) == {"domain_table", "bedrock_migrations"}

    @pytest.mark.parametrize(
        "case", ["missing", "nonstring", "empty", "unknown", "no_models", "conflict", "shared", "unloaded"]
    )
    def test_invalid_ownership_prevents_first_revision(
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
        app = _model_app(module_factory, "ow_invalid", models)
        registry = ModuleRegistry()
        registry.populate([app, other])
        db_path = tmp_path / "invalid.db"
        with pytest.raises(MigrationError, match="invalid"):
            MigrationsManager(registry, _database_url(db_path)).revision(app, "initial schema")
        assert list((module_factory.root / app / "migrations").glob("*.py")) == []
        assert not _tables(db_path) - {"bedrock_migrations"}

    def test_empty_models_do_not_create_foreign_tables(self, module_factory: ModuleFactory, tmp_path: Path) -> None:
        core = _model_app(module_factory, "ow_core", _model_source("Account", "account"))
        empty = _model_app(module_factory, "ow_empty", "")
        registry = ModuleRegistry()
        registry.populate([core, empty])
        db_path = tmp_path / "empty.db"
        manager = MigrationsManager(registry, _database_url(db_path))
        _install(manager, empty)

        assert _tables(db_path) == {"bedrock_migrations"}
        assert _versions(db_path) == _head_keys(manager, empty)

    def test_inheritance_keeps_table_owners(self, module_factory: ModuleFactory, tmp_path: Path) -> None:
        parent = _model_app(
            module_factory,
            "ow_parent",
            _model_source("Parent", "parent") + "class LocalChild(Parent):\n    pass\n",
        )
        child = _model_app(
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
        _install(manager, parent)
        assert _tables(db_path) == {"parent", "bedrock_migrations"}
        _install(manager, child)
        assert _tables(db_path) == {"parent", "child", "bedrock_migrations"}

    @pytest.mark.parametrize("explicit_owner", [False, True])
    def test_known_shared_table_owners_cannot_be_overridden(
        self, module_factory: ModuleFactory, tmp_path: Path, explicit_owner: bool
    ) -> None:
        core = _model_app(
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

        with pytest.raises(MigrationError, match="multiple apps"):
            manager.revision(core, "initial schema")
        assert list((module_factory.root / core / "migrations").glob("*.py")) == []
        assert not _tables(db_path) - {"bedrock_migrations"}

        registry.install(extension)
        with pytest.raises(MigrationError, match="multiple apps"):
            manager.revision(core, "explicit owner cannot override a conflict")

    def test_external_model_cannot_be_claimed_by_target(self, module_factory: ModuleFactory, tmp_path: Path) -> None:
        target = _model_app(module_factory, "ow_target", _model_source("Local", "local"))
        other = _model_app(
            module_factory,
            "ow_billing",
            _model_source("Invoice", "invoice", "    __table_args__ = {'info': {'bedrock_module': 'ow_target'}}\n"),
        )
        registry = ModuleRegistry()
        registry.populate([target])
        ModuleRegistry().populate([other])
        db_path = tmp_path / "external.db"
        with pytest.raises(MigrationError, match="declares owner 'ow_target'.*belong to 'ow_billing'"):
            MigrationsManager(registry, _database_url(db_path)).revision(target, "initial")
        assert not list((module_factory.root / target / "migrations").glob("*.py"))
        assert not _tables(db_path)


@pytest.fixture
def schema_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[sa.Engine]:
    """Use one real engine with a persistent attached SQLite schema."""
    engine = sa.create_engine(_database_url(tmp_path / "schemas.db"))

    @sa.event.listens_for(engine, "connect")
    def attach(connection: sqlite3.Connection, _record: object) -> None:
        connection.execute("ATTACH DATABASE ? AS archive", (str(tmp_path / "archive.db"),))

    monkeypatch.setattr("bedrock.database.migration.manager.create_engine", lambda *args, **kwargs: engine)
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
        _baseline(manager, core, plugin)
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
        _baseline(manager, core, plugin)
        own, foreign = ("account", "tag") if target == core else ("tag", "account")
        if foreign_exists:
            with sqlite3.connect(db_path) as connection:
                connection.execute(f"CREATE TABLE {foreign} (id INTEGER NOT NULL PRIMARY KEY)")
                connection.execute(f"INSERT INTO {foreign} VALUES (42)")
        manager.revision(target, "create owned table")
        manager.upgrade(target)
        expected = {"bedrock_migrations", foreign} if foreign_exists else {"bedrock_migrations"}
        assert _tables(db_path) == expected | {own}
        manager.downgrade(target, "-1")
        assert _tables(db_path) == expected
        assert _versions(db_path) == {(core, "ag_core0"), (plugin, "ag_plugin0")}
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
        _baseline(manager, core, plugin)
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
        core = _model_app(module_factory, "ag_core", _model_source("Account", "account"))
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
        _install(manager, core)
        _baseline(manager, plugin)
        manager.revision(plugin, "add account links")
        manager.upgrade(plugin)
        with sqlite3.connect(db_path) as connection:
            assert [(row[2], row[3], row[4]) for row in connection.execute("PRAGMA foreign_key_list(links)")] == [
                ("account", "user_id", "id")
            ]
        manager.downgrade(plugin, "-1")
        assert _tables(db_path) == {"account", "bedrock_migrations"}

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
        _baseline(manager, app, other)
        with pytest.raises(MigrationError):
            manager.revision(app, "must not be written")
        assert {p.name for p in (module_factory.root / app / "migrations").glob("*.py")} == {"initial.py"}
        assert _versions(db_path) == {(app, "ag_invalid0"), (other, "ag_other0")}
        assert _tables(db_path) == {"bedrock_migrations"}

    def test_empty_module_generates_no_foreign_operations(self, module_factory: ModuleFactory, tmp_path: Path) -> None:
        core = _migration_app(module_factory, "ag_core", _model_source("Account", "account"))
        empty = _migration_app(module_factory, "ag_empty", "")
        registry = ModuleRegistry()
        registry.populate([core, empty])
        db_path = tmp_path / "empty_generation.db"
        manager = MigrationsManager(registry, _database_url(db_path))
        _baseline(manager, core, empty)
        manager.revision(empty, "no owned changes")
        manager.upgrade(empty)
        manager.uninstall(empty)
        assert _tables(db_path) == {"bedrock_migrations"}
        assert _versions(db_path) == {(core, "ag_core0")}

    @pytest.mark.parametrize("schema", [None, "main"])
    def test_default_schema_aliases_do_not_create_phantom_tables(
        self, module_factory: ModuleFactory, tmp_path: Path, schema: str | None
    ) -> None:
        app = _model_app(
            module_factory,
            "ag_app",
            _model_source("Account", "account", f"    __table_args__ = {{'schema': {schema!r}}}\n"),
        )
        registry = ModuleRegistry()
        registry.populate([app])
        db_path = tmp_path / "default_schema.db"
        manager = MigrationsManager(registry, _database_url(db_path))
        _install(manager, app)
        with sqlite3.connect(db_path) as connection:
            connection.execute("INSERT INTO account VALUES (42)")
        manager.revision(app, "no changes")
        manager.upgrade(app)
        manager.downgrade(app, "-1")
        assert _sqlite_rows(db_path, "SELECT id FROM account") == {42}

    def test_install_uninstall_preserves_foreign_data_and_revision(
        self, module_factory: ModuleFactory, tmp_path: Path
    ) -> None:
        core = _model_app(module_factory, "ag_core", _model_source("Account", "account"))
        plugin = module_factory("ag_plugin", models=_model_source("Tag", "tag"), files={"migrations/__init__.py": ""})
        registry = ModuleRegistry()
        registry.populate([core, plugin])
        db_path = tmp_path / "lifecycle.db"
        manager = MigrationsManager(registry, _database_url(db_path))
        _install(manager, core)
        with sqlite3.connect(db_path) as connection:
            connection.execute("INSERT INTO account VALUES (42)")
        manager.revision(plugin, "initial plugin")
        assert manager.ensure_schema(plugin) == "created"
        assert _tables(db_path) == {"account", "tag", "bedrock_migrations"}
        manager.uninstall(plugin)
        assert _tables(db_path) == {"account", "bedrock_migrations"}
        assert _sqlite_rows(db_path, "SELECT id FROM account") == {42}
        assert _versions(db_path) == _head_keys(manager, core)

    def test_removed_model_requires_explicit_drop(self, module_factory: ModuleFactory, tmp_path: Path) -> None:
        app = _migration_app(module_factory, "ag_app", "")
        registry = ModuleRegistry()
        registry.populate([app])
        db_path = tmp_path / "historical.db"
        manager = MigrationsManager(registry, _database_url(db_path))
        _baseline(manager, app)
        with sqlite3.connect(db_path) as connection:
            connection.execute("CREATE TABLE removed (id INTEGER NOT NULL PRIMARY KEY)")
            connection.execute("INSERT INTO removed VALUES (42)")
        manager.revision(app, "no inferred drop")
        manager.upgrade(app)
        assert _sqlite_rows(db_path, "SELECT id FROM removed") == {42}
        revision = manager.heads(app)[0]
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

    def test_revision_requires_app_at_head(self, module_factory: ModuleFactory, tmp_path: Path) -> None:
        app = _migration_app(module_factory, "ag_app", "")
        registry = ModuleRegistry()
        registry.populate([app])
        manager = MigrationsManager(registry, _database_url(tmp_path / "behind.db"))

        with pytest.raises(MigrationError, match="not at head"):
            manager.revision(app, "pending revision exists")

        assert {p.name for p in (module_factory.root / app / "migrations").glob("*.py")} == {"initial.py"}

    def test_revision_records_dependency_heads_once(self, module_factory: ModuleFactory, tmp_path: Path) -> None:
        auth = _model_app(module_factory, "dp_auth", "")
        orders = _model_app(
            module_factory,
            "dp_orders",
            "",
            manifest={"title": "Orders", "version": "0.1.0", "depends_on": [auth]},
        )
        registry = ModuleRegistry()
        registry.populate([auth, orders])
        manager = MigrationsManager(registry, _database_url(tmp_path / "deps.db"))

        def generate(app: str, message: str) -> str:
            """Generate and run a revision; return its recorded depends_on."""
            manager.revision(app, message)
            manager.upgrade(app)
            head = manager.heads(app)[0]
            return {entry["revision"]: entry["depends_on"] for entry in manager.get_history(app)}[head]

        assert generate(auth, "initial") == ""
        auth_first = manager.heads(auth)[0]
        assert generate(orders, "initial") == f"{auth}:{auth_first}"
        assert generate(orders, "second") == ""
        generate(auth, "email")
        assert generate(orders, "third") == f"{auth}:{manager.heads(auth)[0]}"
