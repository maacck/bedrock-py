"""Database selection and noncreating SQLite access of squash previews and read-only commands."""

from pathlib import Path

import pytest
import sqlalchemy as sa
from bedrock.database import MigrationError, MigrationsManager
from bedrock.database.migration.ledger import MigrationLedger
from bedrock.database.migration.manager import _MissingDatabase
from bedrock.module.registry import ModuleRegistry
from bedrock.testing.modules import ModuleFactory


@pytest.fixture(autouse=True)
def isolated_migration_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    from bedrock.database.base import BedrockModel

    monkeypatch.setattr(BedrockModel, "metadata", sa.MetaData())


def _manager(factory: ModuleFactory, url: str) -> MigrationsManager:
    app = factory("conn_app", manifest={"title": "c", "version": "0.1.0"}, models="")
    registry = ModuleRegistry()
    registry.populate([app])
    return MigrationsManager(registry, url)


def _urls(db: Path) -> dict[str, str]:
    path = db.as_posix()
    return {
        "path": f"sqlite:///{path}",
        "uri": f"sqlite:///file:{path}?uri=true",
        "uri-rwc": f"sqlite:///file:{path}?mode=rwc&uri=true",
        "uri-inline": f"sqlite:///file:{path}?mode=rwc&cache=private&uri=true",
    }


@pytest.mark.parametrize("kind", ["path", "uri", "uri-rwc", "uri-inline"])
def test_missing_sqlite_file_is_never_created_by_read_only_commands(
    module_factory: ModuleFactory, tmp_path: Path, kind: str
) -> None:
    db = tmp_path / "missing.db"
    manager = _manager(module_factory, _urls(db)[kind])
    assert manager.current("conn_app") == ()
    assert [(s.current, s.state) for s in manager.check("conn_app")] == [((), "unknown")]
    with pytest.raises(_MissingDatabase), manager._squash_transaction(create=False):
        pass
    assert not db.exists()


@pytest.mark.parametrize("kind", ["path", "uri", "uri-rwc", "uri-inline"])
def test_existing_sqlite_file_is_read_without_change(module_factory: ModuleFactory, tmp_path: Path, kind: str) -> None:
    db = tmp_path / "there.db"
    with sa.create_engine(f"sqlite:///{db.as_posix()}").begin() as conn:
        MigrationLedger("bedrock_migrations").ensure_table(conn)
    before = db.read_bytes()
    manager = _manager(module_factory, _urls(db)[kind])
    assert manager.current("conn_app") == ()
    assert manager.check("conn_app")[0].current == ()
    with manager._ledger_reader() as conn:
        assert conn is not None
        with pytest.raises(sa.exc.OperationalError, match="readonly"):
            conn.exec_driver_sql("CREATE TABLE x (id INTEGER)")
    assert db.read_bytes() == before


def test_relative_path_is_not_created(
    module_factory: ModuleFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    manager = _manager(module_factory, "sqlite:///rel.db")
    assert manager.current("conn_app") == ()
    assert not (tmp_path / "rel.db").exists()


@pytest.mark.parametrize(
    "url", ["sqlite://", "sqlite:///:memory:", "sqlite:///file:connmem?mode=memory&cache=shared&uri=true"]
)
def test_memory_databases_keep_their_semantics(
    module_factory: ModuleFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.chdir(tmp_path)
    assert _manager(module_factory, url).current("conn_app") == ()
    assert not (tmp_path / "connmem").exists()


@pytest.mark.parametrize("key", ["dbname", "database", "db"])
@pytest.mark.parametrize("url", ["postgresql://u:s3cret@h/prod?{}=other", "mysql+pymysql://u:s3cret@h/prod?{}=other"])
def test_conflicting_database_selector_is_rejected_before_connecting(
    module_factory: ModuleFactory, monkeypatch: pytest.MonkeyPatch, url: str, key: str
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("connected")

    monkeypatch.setattr(sa.engine.Engine, "connect", refuse)
    monkeypatch.setattr(sa, "create_engine", refuse)
    manager = _manager(module_factory, url.format(key))
    for call in (
        lambda: manager.apply_squash("conn_app", "0001", apply=False),
        lambda: manager.apply_squash("conn_app", "0001", apply=True),
        lambda: manager.current("conn_app"),
        lambda: manager.check("conn_app"),
        lambda: manager._squash_transaction().__enter__(),
    ):
        with pytest.raises(MigrationError, match=key) as error:
            call()
        assert "s3cret" not in str(error.value)


def test_matching_database_selector_is_accepted(module_factory: ModuleFactory) -> None:
    manager = _manager(module_factory, "postgresql://u:p@h/prod?dbname=prod")
    manager._reject_database_selector()
