from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from bedrock.database.migration import ledger
from bedrock.database.migration.graph import Migration, MigrationError, MigrationGraph
from bedrock.database.migration.ledger import MigrationLedger

L = MigrationLedger("bedrock_migrations")


def _mig(app: str, revision: str, down: tuple[str, ...] = (), deps: tuple[tuple[str, str], ...] = ()) -> Migration:
    return Migration(
        app=app,
        revision=revision,
        down_revisions=tuple((app, d) for d in down),
        depends_on=deps,
        message="",
        path=Path(f"{revision}.py"),
        module=ModuleType(revision),
    )


def _graph(*migs: Migration) -> MigrationGraph:
    return MigrationGraph({m.key: m for m in migs})


@pytest.fixture
def graph() -> MigrationGraph:
    return _graph(
        _mig("auth", "a1"),
        _mig("auth", "a2", ("a1",)),
        _mig("orders", "o1", deps=(("auth", "a2"),)),
    )


@pytest.fixture
def engine(tmp_path: Path):
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    yield eng
    eng.dispose()


def _legacy(engine: sa.Engine, *heads: str) -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
        for head in heads:
            conn.exec_driver_sql("INSERT INTO alembic_version VALUES (?)", (head,))


def _tables(engine: sa.Engine) -> set[str]:
    return set(sa.inspect(engine).get_table_names())


def _applied(engine: sa.Engine, led: MigrationLedger = L):
    with engine.connect() as conn:
        return led.read_applied(conn)


def test_names_and_constants():
    assert (L.name, L.staging_name) == (ledger.VERSION_TABLE_NAME, ledger.STAGING_TABLE_NAME)
    custom = MigrationLedger("tenant_a_migrations")
    assert (custom.name, custom.staging_name) == ("tenant_a_migrations", "tenant_a_migrations_adopt")
    assert custom._table.name == custom.name and custom._staging.name == custom.staging_name
    assert L._table.name == "bedrock_migrations"  # building another ledger mutates nothing global


def test_default_stage_marker_preserved_and_custom_markers_unique():
    a, b = MigrationLedger("ledger_a"), MigrationLedger("ledger_b")
    assert L._stage_marker == "bedrock_adopt_v1_origin"
    assert len({L._stage_marker, a._stage_marker, b._stage_marker}) == 3
    longest = MigrationLedger("x" * 57)
    assert len(longest.staging_name) <= 63 and len(longest._stage_marker) <= 64


@pytest.mark.parametrize(
    "name",
    ["", "Upper", "has-dash", "1leading", "dotted.name", "has space", "x_adopt", "alembic_version", "x" * 58, "é"],
)
def test_invalid_version_table_is_rejected(name):
    with pytest.raises(MigrationError, match="Invalid migration version table"):
        MigrationLedger(name)


def test_fresh_database_has_no_ledger_and_reads_empty(engine):
    assert _applied(engine) == {}
    assert _tables(engine) == set()


def test_record_forget_roundtrip(engine, graph):
    with engine.begin() as conn:
        L.ensure_table(conn)
        L.ensure_table(conn)  # idempotent
        L.record(conn, graph[("orders", "o1")])
        L.record(conn, graph[("auth", "a2")])
    applied = _applied(engine)
    assert applied == {
        ("orders", "o1"): ledger.AppliedMigration("orders", "o1", (("auth", "a2"),), "executed"),
        ("auth", "a2"): ledger.AppliedMigration("auth", "a2", (("auth", "a1"),), "executed"),
    }
    with engine.begin() as conn:
        L.forget(conn, ("orders", "o1"))
        L.forget(conn, ("auth", "a2"))
    assert _applied(engine) == {}


def test_custom_ledger_is_isolated_from_default(engine, graph):
    custom = MigrationLedger("custom_migrations")
    with engine.begin() as conn:
        L.ensure_table(conn)
        custom.ensure_table(conn)
        custom.record(conn, graph[("auth", "a1")])
    assert _tables(engine) == {"bedrock_migrations", "custom_migrations"}
    assert set(_applied(engine, custom)) == {("auth", "a1")}
    assert _applied(engine) == {}


@pytest.mark.parametrize("version_table", ["bedrock_migrations", "custom_migrations"])
def test_legacy_state_requires_adopt_and_creates_nothing(engine, version_table):
    led = MigrationLedger(version_table)
    _legacy(engine, "a1")
    with engine.begin() as conn, pytest.raises(MigrationError, match="adopt"):
        led.read_applied(conn)
    with engine.begin() as conn, pytest.raises(MigrationError, match="adopt"):
        led.ensure_table(conn)
    assert _tables(engine) == {"alembic_version"}


def test_unreadable_ledger_is_an_error_not_empty(engine):
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE bedrock_migrations (x INTEGER)")
    with pytest.raises(MigrationError, match="not a Bedrock migration ledger"):
        _applied(engine)


def test_unknown_origin_and_bad_parents_are_errors(engine, graph):
    with engine.begin() as conn:
        L.ensure_table(conn)
        L.record(conn, graph[("auth", "a1")])
        conn.exec_driver_sql("UPDATE bedrock_migrations SET origin = 'weird'")
    with pytest.raises(MigrationError, match="unknown origin"):
        _applied(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("UPDATE bedrock_migrations SET origin = 'executed', parents = 'not json'")
    with pytest.raises(MigrationError, match="Cannot read"):
        _applied(engine)


def test_parents_are_unique_sorted_union_of_both_edge_kinds(engine):
    mixed = _mig("orders", "o2", ("o1", "o0"), (("auth", "a2"), ("auth", "a1"), ("orders", "o1")))
    assert mixed.parents == (("auth", "a1"), ("auth", "a2"), ("orders", "o0"), ("orders", "o1"))
    with engine.begin() as conn:
        L.ensure_table(conn)
        L.record(conn, mixed)
    assert _applied(engine)[("orders", "o2")].parents == mixed.parents


def test_pre_parents_ledger_is_rejected_by_shape(engine):
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE bedrock_migrations (app VARCHAR(255), revision VARCHAR(64), depends_on TEXT NOT NULL, "
            "origin VARCHAR(8) NOT NULL, applied_at DATETIME NOT NULL, PRIMARY KEY (app, revision))"
        )
    with pytest.raises(MigrationError, match="not a Bedrock migration ledger"):
        _applied(engine)


def test_adopt_expands_baseline_with_qualified_keys(engine, graph):
    _legacy(engine, "o1")
    assert L.adopt(engine, graph) == 3
    applied = _applied(engine)
    assert set(applied) == {("auth", "a1"), ("auth", "a2"), ("orders", "o1")}
    assert {a.origin for a in applied.values()} == {"baseline"}
    assert applied[("orders", "o1")].parents == (("auth", "a2"),)
    assert applied[("auth", "a2")].parents == (("auth", "a1"),)  # same-module parent persisted too
    assert applied[("auth", "a1")].parents == ()
    assert _tables(engine) == {"alembic_version", "bedrock_migrations"}
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT version_num FROM alembic_version").all() == [("o1",)]


def test_adopt_into_custom_table_leaves_default_untouched(engine, graph):
    custom = MigrationLedger("custom_migrations")
    _legacy(engine, "o1")
    assert custom.adopt(engine, graph) == 3
    assert _tables(engine) == {"alembic_version", "custom_migrations"}
    assert len(_applied(engine, custom)) == 3
    assert _applied(engine, custom)[("orders", "o1")].origin == "baseline"


def test_adopt_multiple_heads(engine, graph):
    _legacy(engine, "a1", "o1")
    assert L.adopt(engine, graph) == 3


def test_adopt_ambiguous_head_writes_nothing(engine):
    _legacy(engine, "x1")
    graph = _graph(_mig("a", "x1"), _mig("b", "x1"))
    with pytest.raises(MigrationError, match="ambiguous"):
        L.adopt(engine, graph)
    assert _tables(engine) == {"alembic_version"}


def test_adopt_unresolved_head_writes_nothing(engine, graph):
    _legacy(engine, "zzz")
    with pytest.raises(MigrationError, match="zzz"):
        L.adopt(engine, graph)
    assert _tables(engine) == {"alembic_version"}


def _squashed_graph() -> MigrationGraph:
    end = _mig("a", "e1")
    end.module.bedrock_squash = {"format": 1, "old": [("x1", []), ("e1", [])]}
    return _graph(end, _mig("b", "x1"), _mig("b", "b2", ("x1",)))


def test_adopt_head_retired_in_other_app_is_not_misattributed(engine):
    _legacy(engine, "x1")
    with pytest.raises(MigrationError, match="retired by a squash in a"):
        L.adopt(engine, _squashed_graph())
    assert _tables(engine) == {"alembic_version"}


def test_adopt_independent_head_beside_squash_is_preserved(engine):
    _legacy(engine, "b2")
    assert L.adopt(engine, _squashed_graph()) == 2


def test_adopt_without_legacy_table_is_rejected(engine, graph):
    with pytest.raises(MigrationError, match="nothing to adopt"):
        L.adopt(engine, graph)
    assert _tables(engine) == set()


def test_adopt_empty_legacy_table_publishes_empty_ledger_once(engine, graph):
    _legacy(engine)
    assert L.adopt(engine, graph) == 0
    assert _applied(engine) == {}
    with pytest.raises(MigrationError, match="already"):
        L.adopt(engine, graph)
    assert _applied(engine) == {}  # a legitimately empty ledger is not re-imported


def test_adopt_twice_is_rejected_and_keeps_rows(engine, graph):
    _legacy(engine, "o1")
    L.adopt(engine, graph)
    with pytest.raises(MigrationError, match="already"):
        L.adopt(engine, graph)
    assert len(_applied(engine)) == 3


def test_adopt_rejects_conflicting_final_table_without_dropping(engine, graph):
    _legacy(engine, "o1")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE bedrock_migrations (x INTEGER)")
    with pytest.raises(MigrationError, match="not a Bedrock migration ledger"):
        L.adopt(engine, graph)
    assert "bedrock_migrations" in _tables(engine)
    assert "bedrock_migrations_adopt" not in _tables(engine)


@pytest.mark.parametrize("stage", ["_fill_staging", "_publish"])
def test_interrupt_before_publish_leaves_final_absent_and_retry_works(engine, graph, monkeypatch, stage):
    _legacy(engine, "o1")

    def boom(*args, **kwargs):
        raise RuntimeError("injected")

    monkeypatch.setattr(MigrationLedger, stage, boom)
    with pytest.raises(RuntimeError, match="injected"):
        L.adopt(engine, graph)
    monkeypatch.undo()

    fresh = sa.create_engine(engine.url)
    try:
        assert "bedrock_migrations" not in _tables(fresh)
        with pytest.raises(MigrationError, match="adopt"):
            _applied(fresh)  # ordinary migration commands stay refused
        assert L.adopt(fresh, graph) == 3
        assert "bedrock_migrations_adopt" not in _tables(fresh)
        assert len(_applied(fresh)) == 3
    finally:
        fresh.dispose()


def test_interrupt_after_staging_created_only(engine, graph, monkeypatch):
    _legacy(engine, "o1")
    monkeypatch.setattr(MigrationLedger, "_fill_staging", lambda *a: (_ for _ in ()).throw(RuntimeError("injected")))
    with pytest.raises(RuntimeError):
        L.adopt(engine, graph)
    monkeypatch.undo()
    assert "bedrock_migrations_adopt" in _tables(engine)
    assert L.adopt(engine, graph) == 3


def test_interrupt_partially_filled_staging_is_rebuilt(engine, graph):
    _legacy(engine, "o1")
    L._create_staging(engine, replace=False)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO bedrock_migrations_adopt VALUES ('auth', 'a1', '[]', 'baseline', '2026-01-01')"
        )
    assert L.adopt(engine, graph) == 3
    assert len(_applied(engine)) == 3


def test_interrupt_after_rename_before_return_is_published_and_not_reimported(engine, graph, monkeypatch):
    _legacy(engine, "o1")
    real = MigrationLedger._publish

    def publish_then_die(self, eng):
        real(self, eng)
        raise RuntimeError("injected")

    monkeypatch.setattr(MigrationLedger, "_publish", publish_then_die)
    with pytest.raises(RuntimeError, match="injected"):
        L.adopt(engine, graph)
    monkeypatch.undo()

    fresh = sa.create_engine(engine.url)
    try:
        before = _applied(fresh)
        assert len(before) == 3
        with pytest.raises(MigrationError, match="already"):
            L.adopt(fresh, graph)
        assert _applied(fresh) == before
    finally:
        fresh.dispose()


@pytest.mark.parametrize(
    "ddl",
    [
        "CREATE TABLE bedrock_migrations_adopt (x INTEGER)",
        "CREATE TABLE bedrock_migrations_adopt (app VARCHAR(255), revision VARCHAR(64), depends_on TEXT, "
        "origin VARCHAR(8), applied_at DATETIME)",  # right columns, no primary key
    ],
)
def test_staging_of_unknown_origin_is_rejected_untouched(engine, graph, ddl):
    _legacy(engine, "o1")
    with engine.begin() as conn:
        conn.exec_driver_sql(ddl)
    with pytest.raises(MigrationError, match="not left by an interrupted adopt"):
        L.adopt(engine, graph)
    assert "bedrock_migrations" not in _tables(engine)
    assert "bedrock_migrations_adopt" in _tables(engine)


def test_staging_with_executed_rows_is_rejected(engine, graph):
    _legacy(engine, "o1")
    L._create_staging(engine, replace=False)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO bedrock_migrations_adopt VALUES ('auth', 'a1', '[]', 'executed', '2026-01-01')"
        )
    with pytest.raises(MigrationError, match="not left by an interrupted adopt"):
        L.adopt(engine, graph)
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT count(*) FROM bedrock_migrations_adopt").scalar() == 1


def test_two_targets_stage_independently_and_never_touch_each_others_staging(engine, graph):
    first, second = MigrationLedger("first_migrations"), MigrationLedger("second_migrations")
    _legacy(engine, "o1")
    first._create_staging(engine, replace=False)  # interrupted adopt of the first target
    assert second.adopt(engine, graph) == 3
    assert _tables(engine) == {"alembic_version", "first_migrations_adopt", "second_migrations"}
    assert first.adopt(engine, graph) == 3  # rebuilds only its own staging
    assert _tables(engine) == {"alembic_version", "first_migrations", "second_migrations"}


def test_staging_of_another_target_is_not_accepted_as_own(engine, graph):
    # Same shape and baseline rows, but carries the other ledger's marker.
    other, mine = MigrationLedger("other_migrations"), MigrationLedger("mine_migrations")
    _legacy(engine, "o1")
    with engine.begin() as conn:
        other._staging.to_metadata(sa.MetaData(), name=mine.staging_name).create(conn)
    with pytest.raises(MigrationError, match="not left by an interrupted adopt"):
        mine.adopt(engine, graph)
    assert "mine_migrations" not in _tables(engine)
