"""End-to-end tests for ``bedrock db`` with module-scoped migrations.

Most tests run the CLI in a fresh process against a SQLite file, so module loading, exit codes,
printed steps and the resulting database state are exercised exactly as a user sees them.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from bedrock.cli.db import db_app
from bedrock.module.registry import ModuleRegistry
from bedrock.testing.modules import ModuleFactory
from typer.testing import CliRunner

_ENTRY = "from bedrock.cli.main import entrypoint; entrypoint()"


def _script(rev: str, down: str | None, depends_on: tuple[str, ...] = ()) -> str:
    """A revision that creates (and drops) a table named after itself."""
    return (
        "from alembic import op\nimport sqlalchemy as sa\n"
        f"revision = {rev!r}\ndown_revision = {down!r}\ndepends_on = {depends_on!r}\n"
        f"def upgrade():\n    op.create_table({rev!r}, sa.Column('id', sa.Integer, primary_key=True))\n"
        f"def downgrade():\n    op.drop_table({rev!r})\n"
    )


class _Project:
    """Temporary modules plus a SQLite database, driven through fresh CLI processes."""

    def __init__(self, factory: ModuleFactory, db_path: Path) -> None:
        self.factory = factory
        self.db_path = db_path
        self.env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join([str(factory.root), *filter(None, [os.environ.get("PYTHONPATH")])]),
            "DATABASE_TYPE": "sqlite",
            "DATABASE_DRIVER": "pysqlite",
            "DATABASE_SCHEMA": str(db_path),
            "COLUMNS": "200",
            "NO_COLOR": "1",
        }

    def app(self, name: str, scripts: dict[str, str], depends_on: tuple[str, ...] = ()) -> str:
        """Create a module whose migration files are named after their revision."""
        return self.factory(
            name,
            manifest={"title": name, "version": "0.1.0", "depends_on": list(depends_on)},
            models="",
            files={f"migrations/{rev}.py": src for rev, src in scripts.items()},
        )

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run ``bedrock db <args>`` in a new process."""
        return subprocess.run(
            [sys.executable, "-c", _ENTRY, "db", *args],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def sql(self, statement: str) -> list[tuple]:
        """Run *statement* on the database file and return all rows."""
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(statement).fetchall()

    def tables(self) -> set[str]:
        """Business tables, excluding both version tables."""
        rows = self.sql("SELECT name FROM sqlite_master WHERE type = 'table'")
        return {name for (name,) in rows} - {"bedrock_migrations", "alembic_version"}

    def versions(self) -> set[tuple[str, str]]:
        """``(app, revision)`` keys recorded in ``bedrock_migrations``."""
        return set(self.sql("SELECT app, revision FROM bedrock_migrations"))


def _steps(proc: subprocess.CompletedProcess[str]) -> list[str]:
    return proc.stdout.split()


@pytest.fixture
def project(module_factory: ModuleFactory, tmp_path: Path) -> _Project:
    """auth (a1 -> a2); orders (o1) and billing (b1) both depend on ``auth:a2``."""
    proj = _Project(module_factory, tmp_path / "cli.db")
    proj.app("mc_auth", {"a1": _script("a1", None), "a2": _script("a2", "a1")})
    for name, rev in (("mc_orders", "o1"), ("mc_billing", "b1")):
        proj.app(name, {rev: _script(rev, None, ("mc_auth:a2",))}, depends_on=("mc_auth",))
    return proj


class TestUpgradeOutput:
    def test_prints_executed_steps_then_nothing_to_do(self, project: _Project) -> None:
        first = project.cli("upgrade", "mc_orders")
        assert first.returncode == 0, first.stdout + first.stderr
        assert _steps(first) == ["mc_auth:a1", "mc_auth:a2", "mc_orders:o1"]
        assert project.versions() == {("mc_auth", "a1"), ("mc_auth", "a2"), ("mc_orders", "o1")}

        second = project.cli("upgrade", "mc_orders")
        assert second.returncode == 0
        assert second.stdout.strip() == "Nothing to do."

    def test_heads_current_and_history(self, project: _Project) -> None:
        heads = project.cli("heads", "mc_orders")
        assert heads.returncode == 0
        assert _steps(heads) == ["o1"]

        before = project.cli("current", "mc_orders")
        assert before.returncode == 0
        assert _steps(before) == ["-"]
        assert not project.sql("SELECT name FROM sqlite_master")  # read-only commands create nothing

        project.cli("upgrade", "mc_orders")
        assert _steps(project.cli("current", "mc_orders")) == ["o1"]

        history = project.cli("history", "mc_orders")
        assert history.returncode == 0
        header, *rows = history.stdout.splitlines()
        assert all(column in header for column in ("Revision", "Down Revision", "Depends On", "Message"))
        assert "Branch Labels" not in header
        assert "o1 (head)" in rows[0]
        assert "mc_auth:a2" in rows[0]

    def test_unknown_target_exits_nonzero(self, project: _Project) -> None:
        proc = project.cli("upgrade", "mc_orders", "nope")
        assert proc.returncode == 1
        assert "nope" in proc.stdout

    def test_help_lists_adopt_and_cascade(self, project: _Project) -> None:
        listing = project.cli("--help")
        assert listing.returncode == 0
        assert "adopt" in listing.stdout
        assert "Database migration commands for Bedrock modules." in listing.stdout
        assert "--cascade" in project.cli("downgrade", "--help").stdout
        assert "--load" in project.cli("uninstall", "--help").stdout


class TestRollbackDependents:
    @pytest.fixture(autouse=True)
    def _applied(self, project: _Project) -> None:
        assert project.cli("upgrade", "mc_orders").returncode == 0
        self.before_tables = project.tables()
        self.before_versions = project.versions()

    def _unchanged(self, project: _Project) -> None:
        assert project.tables() == self.before_tables
        assert project.versions() == self.before_versions

    def test_unloaded_dependent_is_rejected(self, project: _Project) -> None:
        proc = project.cli("downgrade", "mc_auth", "a1")
        assert proc.returncode == 1
        assert "mc_orders:o1" in proc.stdout
        self._unchanged(project)

    def test_loaded_dependent_requires_cascade(self, project: _Project) -> None:
        proc = project.cli("downgrade", "mc_auth", "a1", "--load", "mc_orders")
        assert proc.returncode == 1
        assert "mc_orders:o1" in proc.stdout
        assert "--cascade" in proc.stdout
        self._unchanged(project)

    def test_cascade_rolls_back_dependents_first(self, project: _Project) -> None:
        proc = project.cli("downgrade", "mc_auth", "a1", "--load", "mc_orders", "--cascade")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert _steps(proc) == ["mc_orders:o1", "mc_auth:a2"]
        assert project.versions() == {("mc_auth", "a1")}
        assert project.tables() == {"a1"}

    def test_load_is_repeatable(self, project: _Project) -> None:
        project.cli("upgrade", "mc_billing")
        proc = project.cli("downgrade", "mc_auth", "a1", "--load", "mc_orders", "--load", "mc_billing", "--cascade")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        steps = _steps(proc)
        assert steps.index("mc_auth:a2") == len(steps) - 1
        assert set(steps[:-1]) == {"mc_orders:o1", "mc_billing:b1"}

    def test_unloaded_third_party_blocks_even_with_cascade(self, project: _Project) -> None:
        assert project.cli("upgrade", "mc_billing").returncode == 0
        before_tables, before_versions = project.tables(), project.versions()

        proc = project.cli("downgrade", "mc_auth", "a1", "--load", "mc_orders", "--cascade")
        assert proc.returncode == 1
        assert "mc_billing:b1" in proc.stdout
        assert project.tables() == before_tables
        assert project.versions() == before_versions

    def test_uninstall_follows_the_same_rules(self, project: _Project) -> None:
        refused = project.cli("uninstall", "mc_auth", "--load", "mc_orders")
        assert refused.returncode == 1
        self._unchanged(project)

        done = project.cli("uninstall", "mc_auth", "--load", "mc_orders", "--cascade")
        assert done.returncode == 0, done.stdout + done.stderr
        assert _steps(done)[:3] == ["mc_orders:o1", "mc_auth:a2", "mc_auth:a1"]
        assert project.versions() == set()
        assert project.tables() == set()


class TestAdopt:
    @pytest.fixture
    def legacy(self, project: _Project) -> _Project:
        """A database that Alembic stamped at ``o1`` and that already holds business data."""
        project.sql("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
        project.sql("INSERT INTO alembic_version VALUES ('o1')")
        for table in ("a1", "a2", "o1"):
            project.sql(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
            project.sql(f"INSERT INTO {table} VALUES (1)")

        return project

    def test_legacy_database_requires_adopt(self, legacy: _Project) -> None:
        proc = legacy.cli("upgrade", "mc_orders")
        assert proc.returncode == 1
        assert "adopt" in proc.stdout
        assert "bedrock_migrations" not in {name for (name,) in legacy.sql("SELECT name FROM sqlite_master")}

    def test_adopt_imports_protected_baseline(self, legacy: _Project) -> None:
        proc = legacy.cli("adopt", "mc_orders")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "3" in proc.stdout
        assert legacy.versions() == {("mc_auth", "a1"), ("mc_auth", "a2"), ("mc_orders", "o1")}
        assert legacy.sql("SELECT version_num FROM alembic_version") == [("o1",)]

        assert legacy.cli("upgrade", "mc_orders").stdout.strip() == "Nothing to do."

        blocked = legacy.cli("downgrade", "mc_auth", "a1", "--load", "mc_orders", "--cascade")
        assert blocked.returncode == 1
        assert "baseline" in blocked.stdout.lower()
        assert len(legacy.versions()) == 3
        assert legacy.sql("SELECT id FROM a2") == [(1,)]

        again = legacy.cli("adopt", "mc_orders")
        assert again.returncode == 1

    def test_adopt_with_unresolved_head_writes_nothing(self, legacy: _Project) -> None:
        legacy.sql("INSERT INTO alembic_version VALUES ('zzz')")

        proc = legacy.cli("adopt", "mc_orders")
        assert proc.returncode == 1
        assert "zzz" in proc.stdout
        assert "bedrock_migrations" not in {name for (name,) in legacy.sql("SELECT name FROM sqlite_master")}


class TestRegistryAlreadyReady:
    """A ready registry cannot be extended, so requested roots must already be loaded."""

    @pytest.fixture
    def ready(self, project: _Project, monkeypatch: pytest.MonkeyPatch) -> None:
        registry = ModuleRegistry()
        registry.populate(["mc_auth"])
        monkeypatch.setattr("bedrock.module.registry.apps", registry)
        for key, value in project.env.items():
            if key.startswith("DATABASE_"):
                monkeypatch.setenv(key, value)
        self.db_path = project.db_path

    def test_adopt_rejects_unloaded_root(self, ready: None) -> None:
        result = CliRunner().invoke(db_app, ["adopt", "mc_orders"])
        assert result.exit_code == 1
        assert "mc_orders" in result.output
        assert not self.db_path.exists()

    def test_load_rejects_unloaded_root_before_planning(self, ready: None) -> None:
        result = CliRunner().invoke(db_app, ["downgrade", "mc_auth", "base", "--load", "mc_orders"])
        assert result.exit_code == 1
        assert "mc_orders" in result.output
        assert not self.db_path.exists()


class TestInitializedDbSettings:
    """An initialized ``db`` url and settings win over the environment in every migration-running command."""

    @pytest.fixture
    def initialized(self, project: _Project, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[_Project]:
        from bedrock.database import db
        from bedrock.database.config import DbSettings

        for key, value in project.env.items():
            if key.startswith("DATABASE_"):
                monkeypatch.setenv(key, value)
        monkeypatch.setenv("DATABASE_SCHEMA", str(tmp_path / "env-only.db"))
        db.init(
            f"sqlite+pysqlite:///{project.db_path}",
            DbSettings(MIGRATION_VERSION_TABLE="custom_migrations", MIGRATION_LOCK_TIMEOUT=5),
        )
        yield project
        db.close()

    @pytest.mark.parametrize("command", ["db", "manage", "apps"])
    def test_applied_migrations_are_not_repeated(
        self, initialized: _Project, monkeypatch: pytest.MonkeyPatch, command: str
    ) -> None:
        from bedrock.cli import apps as cli_apps
        from bedrock.cli import manage as cli_manage

        def run() -> None:
            registry = ModuleRegistry()
            monkeypatch.setattr("bedrock.module.registry.apps", registry)
            monkeypatch.setattr("bedrock.module.apps", registry)
            if command == "db":
                result = CliRunner().invoke(db_app, ["upgrade", "mc_auth"])
                assert result.exit_code == 0, result.output
            elif command == "manage":
                registry.populate(["mc_auth"])
                cli_manage.install(app=None, skip_migrations=False)
            else:
                cli_apps.install("mc_auth", skip_migrations=False)

        run()
        initialized.sql("INSERT INTO a1 VALUES (42)")
        run()  # a repeated a1 migration would fail on the existing table

        assert initialized.sql("SELECT id FROM a1") == [(42,)]
        recorded = set(initialized.sql("SELECT app, revision FROM custom_migrations"))
        assert recorded == {("mc_auth", "a1"), ("mc_auth", "a2")}
        assert "bedrock_migrations" not in {n for (n,) in initialized.sql("SELECT name FROM sqlite_master")}


class TestInitLedger:
    """A new, independent module registry joins a legacy database whose core is already adopted."""

    TARGET = "plugin_versions"

    @pytest.fixture
    def adopted(self, project: _Project) -> _Project:
        project.sql("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
        project.sql("INSERT INTO alembic_version VALUES ('o1')")
        for table in ("a1", "a2", "o1"):
            project.sql(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
        project.factory(
            "mc_plugin",
            manifest={"title": "mc_plugin", "version": "0.1.0", "depends_on": []},
            models=(
                "from sqlalchemy import Column, Integer\n"
                "from bedrock.database.base import BedrockModel\n\n"
                "class Plugin(BedrockModel):\n"
                "    __tablename__ = 'p1'\n"
                "    id = Column(Integer, primary_key=True)\n"
            ),
            files={"migrations/p1.py": _script("p1", None)},
        )
        done = project.cli("adopt", "mc_orders")
        assert done.returncode == 0, done.stdout + done.stderr
        project.env["DATABASE_MIGRATION_VERSION_TABLE"] = self.TARGET
        return project

    @staticmethod
    def _master(proj: _Project) -> list[tuple]:
        return proj.sql("SELECT type, name, sql FROM sqlite_master ORDER BY name")

    def test_ordinary_upgrade_is_refused_until_init_ledger(self, adopted: _Project) -> None:
        core = adopted.sql("SELECT * FROM bedrock_migrations ORDER BY app, revision")
        legacy = adopted.sql("SELECT * FROM alembic_version")

        refused = adopted.cli("upgrade", "mc_plugin")
        assert refused.returncode == 1
        assert "p1" not in adopted.tables()
        assert self.TARGET not in {n for (n,) in adopted.sql("SELECT name FROM sqlite_master")}

        before = self._master(adopted)
        init = adopted.cli("init-ledger", "mc_plugin", "--adopted-ledger", "bedrock_migrations")
        assert init.returncode == 0, init.stdout + init.stderr
        names = {n for (kind, n, _) in self._master(adopted) if kind == "table"}
        assert self.TARGET in names
        assert not {"bedrock_migrations_adopt", self.TARGET + "_adopt"} & names
        assert {n for (kind, n, _) in before if kind == "table"} | {self.TARGET} == names
        assert adopted.sql(f"SELECT * FROM {self.TARGET}") == []
        assert "p1" not in adopted.tables()  # init-ledger runs no migrations

        upgraded = adopted.cli("upgrade", "mc_plugin")
        assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
        assert _steps(upgraded)[:1] == ["mc_plugin:p1"]
        assert adopted.sql(f"SELECT app, revision FROM {self.TARGET}") == [("mc_plugin", "p1")]
        assert adopted.sql("SELECT * FROM bedrock_migrations ORDER BY app, revision") == core
        assert adopted.sql("SELECT * FROM alembic_version") == legacy

    def test_second_init_ledger_is_refused(self, adopted: _Project) -> None:
        args = ("init-ledger", "mc_plugin", "--adopted-ledger", "bedrock_migrations")
        assert adopted.cli(*args).returncode == 0
        before = self._master(adopted)

        again = adopted.cli(*args)
        assert again.returncode == 1
        assert self._master(adopted) == before

    def test_source_rows_for_loaded_module_are_refused(self, adopted: _Project) -> None:
        before = self._master(adopted)
        proc = adopted.cli("init-ledger", "mc_orders", "--adopted-ledger", "bedrock_migrations")
        assert proc.returncode == 1
        assert self._master(adopted) == before
        assert len(adopted.versions()) == 3

    def test_missing_source_adoption_is_refused(self, adopted: _Project) -> None:
        proc = adopted.cli("init-ledger", "mc_plugin", "--adopted-ledger", "unadopted_versions")
        assert proc.returncode == 1
        assert self.TARGET not in {n for (n,) in adopted.sql("SELECT name FROM sqlite_master")}

    def test_existing_model_owned_table_is_refused(self, adopted: _Project) -> None:
        adopted.sql("CREATE TABLE p1 (id INTEGER PRIMARY KEY)")
        before = self._master(adopted)

        proc = adopted.cli("init-ledger", "mc_plugin", "--adopted-ledger", "bedrock_migrations")
        assert proc.returncode == 1
        assert self._master(adopted) == before
