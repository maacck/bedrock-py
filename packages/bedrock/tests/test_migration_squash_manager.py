"""Squash apply through the manager: physical connection, locking, transactions and protected states."""

import sqlite3
import threading
from contextlib import closing, suppress
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from bedrock.database import MigrationError, MigrationsManager
from bedrock.database.migration import squash_writer
from bedrock.database.migration.ledger import MigrationLedger, _encode
from bedrock.database.migration.manager import SquashReport
from bedrock.module.registry import ModuleRegistry
from bedrock.testing.modules import ModuleFactory


@pytest.fixture(autouse=True)
def isolated_migration_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    from bedrock.database.base import BedrockModel

    monkeypatch.setattr(BedrockModel, "metadata", sa.MetaData())


def _script(rev: str, down: str | None, squash: object = None) -> str:
    src = (
        "from alembic import op\nimport sqlalchemy as sa\n"
        f"revision = {rev!r}\ndown_revision = {down!r}\ndepends_on = ()\n"
        f"def upgrade():\n    op.create_table({rev!r}, sa.Column('id', sa.Integer, primary_key=True))\n"
        f"def downgrade():\n    op.drop_table({rev!r})\n"
    )
    return src + (f"bedrock_squash = {squash!r}\n" if squash is not None else "")


def _published(app: str, first: str, middle: str, end: str, base: str, tail: str | None = None) -> dict[str, str]:
    squash = {
        "format": 1,
        "old": [(first, [f"{app}:{base}"]), (middle, [f"{app}:{first}"]), (end, [f"{app}:{middle}"])],
    }
    scripts = {base: _script(base, None), end: _script(end, base, squash)}
    if tail:
        scripts[tail] = _script(tail, end)
    return scripts


class World:
    def __init__(self, factory: ModuleFactory, tmp_path: Path) -> None:
        self.factory = factory
        self.db = tmp_path / "w.db"
        self.url = f"sqlite+pysqlite:////{self.db.as_posix().lstrip('/')}"
        self.orders = factory(
            "sqm_orders",
            manifest={"title": "o", "version": "0.1.0"},
            models="",
            files={
                f"migrations/{r}.py": s
                for r, s in _published(
                    "sqm_orders", "0008_add", "0009_fill", "0010_finalize", "0007_base", "0011_ship"
                ).items()
            },
        )
        self.billing = factory(
            "sqm_billing",
            manifest={"title": "b", "version": "0.1.0"},
            models="",
            files={
                f"migrations/{r}.py": s
                for r, s in _published("sqm_billing", "0002_a", "0003_b", "0004_c", "0001_base").items()
            },
        )

    def manager(self, *apps: str, version_table: str = "bedrock_migrations", timeout: float = 5) -> MigrationsManager:
        registry = ModuleRegistry()
        registry.populate(list(apps or (self.orders, self.billing)))
        return MigrationsManager(registry, self.url, lock_timeout=timeout, version_table=version_table)

    def seed(
        self, rows: list[tuple[str, str, tuple[tuple[str, str], ...], str]], table: str = "bedrock_migrations"
    ) -> None:
        led = MigrationLedger(table)
        with sa.create_engine(self.url).begin() as conn:
            led.ensure_table(conn)
            for day, (app, rev, parents, origin) in enumerate(rows, start=1):
                conn.execute(
                    sa.insert(led._table).values(
                        app=app,
                        revision=rev,
                        parents=_encode(parents),
                        origin=origin,
                        applied_at=datetime(2026, 2, day, tzinfo=UTC),
                    )
                )

    def rows(self, table: str = "bedrock_migrations") -> list[tuple]:
        with closing(sqlite3.connect(self.db)) as c:
            return list(c.execute(f"SELECT * FROM {table} ORDER BY app, revision"))  # noqa: S608

    def tables(self) -> set[str]:
        with closing(sqlite3.connect(self.db)) as c:
            return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def business(self, revs: list[str]) -> None:
        """Business tables that hold one row each; replaying their creation would fail or lose data."""
        with closing(sqlite3.connect(self.db)) as c, c:
            for rev in revs:
                c.execute(f'CREATE TABLE "{rev}" (id INTEGER PRIMARY KEY)')  # noqa: S608
                c.execute(f'INSERT INTO "{rev}" VALUES (1)')  # noqa: S608

    def data(self) -> dict[str, list[tuple]]:
        with closing(sqlite3.connect(self.db)) as c:
            names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            return {n: list(c.execute(f'SELECT * FROM "{n}"')) for n in names if n != "bedrock_migrations"}  # noqa: S608

    def add_script(self, app: str, rev: str, down: str, source: str | None = None) -> None:
        path = self.factory.root / app / "migrations" / f"{rev}.py"
        path.write_text(source or _script(rev, down), encoding="utf-8")


def _old_orders(upto: int = 5) -> list[tuple[str, str, tuple[tuple[str, str], ...], str]]:
    o = "sqm_orders"
    rows = [
        (o, "0007_base", (), "executed"),
        (o, "0008_add", ((o, "0007_base"),), "executed"),
        (o, "0009_fill", ((o, "0008_add"),), "executed"),
        (o, "0010_finalize", ((o, "0009_fill"),), "executed"),
        (o, "0011_ship", ((o, "0010_finalize"),), "executed"),
    ]
    return rows[:upto]


def _old_billing() -> list[tuple[str, str, tuple[tuple[str, str], ...], str]]:
    b = "sqm_billing"
    return [
        (b, "0001_base", (), "executed"),
        (b, "0002_a", ((b, "0001_base"),), "executed"),
        (b, "0003_b", ((b, "0002_a"),), "executed"),
        (b, "0004_c", ((b, "0003_b"),), "executed"),
    ]


@pytest.fixture
def world(module_factory: ModuleFactory, tmp_path: Path) -> World:
    return World(module_factory, tmp_path)


END = ("sqm_orders", "0010_finalize")
BILL_END = ("sqm_billing", "0004_c")


def test_preview_reports_exact_rows_and_writes_nothing(world: World) -> None:
    world.seed(_old_orders())
    before, tables = world.rows(), world.tables()
    report = world.manager(world.orders).apply_squash("sqm_orders", "0010_finalize")
    assert isinstance(report, SquashReport)
    assert (report.state, report.applied, report.key) == ("replace", False, END)
    assert report.update == (END, END)
    assert report.delete == (("sqm_orders", "0008_add"), ("sqm_orders", "0009_fill"))
    assert report.version_table == "bedrock_migrations"
    assert (world.rows(), world.tables()) == (before, tables)


def test_target_keeps_routing_overrides_and_redacts_credentials(world: World) -> None:
    registry = ModuleRegistry()
    url = "postgresql+psycopg2://app:s3cret@db.example:5432/prod?host=replica.example&sslpassword=x&password=y"
    manager = MigrationsManager(registry, url)
    spec = type("S", (), {"end": END, "old": ((END, ()),)})()
    target = manager._squash_report(spec, {END: "current"}, applied=False).target  # type: ignore[arg-type]
    assert target == "postgresql+psycopg2://db.example:5432/prod?host=replica.example"


def test_apply_rewrites_once_then_is_current_without_any_dml(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.seed(_old_orders())
    before = {r[:2]: r for r in world.rows()}
    manager = world.manager(world.orders)
    report = manager.apply_squash("sqm_orders", "0010_finalize", apply=True)
    assert (report.state, report.applied) == ("replace", True)

    after = {r[:2]: r for r in world.rows()}
    assert set(after) == set(before) - {("sqm_orders", "0008_add"), ("sqm_orders", "0009_fill")}
    assert after[("sqm_orders", "0010_finalize")][3:] == before[("sqm_orders", "0010_finalize")][3:]
    assert after[("sqm_orders", "0010_finalize")][2] == _encode((("sqm_orders", "0007_base"),))
    assert after[("sqm_orders", "0011_ship")] == before[("sqm_orders", "0011_ship")]

    calls: list[object] = []
    monkeypatch.setattr(MigrationLedger, "replace_squash", lambda *a: calls.append(a))
    again = manager.apply_squash("sqm_orders", "0010_finalize", apply=True)
    assert (again.state, again.applied, calls) == ("current", False, [])
    assert manager.current("sqm_orders") == ("0011_ship",)  # plain commands work again
    assert manager.upgrade("sqm_orders") == []


OLD_ORDERS = ["0007_base", "0008_add", "0009_fill", "0010_finalize", "0011_ship"]
SHIP = ("sqm_orders", "0011_ship")
TAIL = ("sqm_orders", "0012_more")


def _report(world: World) -> str:
    """An app whose ancestry reaches both squashed ranges."""
    deps = "depends_on = ('sqm_orders:0011_ship', 'sqm_billing:0004_c')"
    script = _script("0001_r", None).replace("depends_on = ()", deps)
    return world.factory(
        "sqm_report",
        manifest={"title": "r", "version": "0.1.0", "depends_on": ["sqm_orders", "sqm_billing"]},
        models="",
        files={"migrations/0001_r.py": script},
    )


def test_upgrade_reconciles_old_production_ledger_then_runs_only_the_new_tail(world: World) -> None:
    world.seed(_old_orders())
    world.business(OLD_ORDERS)
    world.add_script("sqm_orders", "0012_more", "0011_ship")
    data, before = world.data(), {r[:2]: r for r in world.rows()}
    manager = world.manager(world.orders)

    assert manager.upgrade("sqm_orders") == [TAIL]

    rows = {r[:2]: r for r in world.rows()}
    assert {k[1] for k in rows} == {"0007_base", "0010_finalize", "0011_ship", "0012_more"}
    assert rows[SHIP] == before[SHIP] and rows[END][3:] == before[END][3:]  # origin and applied_at survive
    assert world.data() == {**data, "0012_more": []}  # nothing replayed, every row preserved
    assert manager.upgrade("sqm_orders") == [] and {r[:2]: r for r in world.rows()} == rows


def test_failed_new_migration_keeps_data_and_retry_runs_only_it(world: World) -> None:
    world.seed(_old_orders())
    world.business(OLD_ORDERS)
    broken = _script("0012_more", "0011_ship").replace("op.create_table(", "raise RuntimeError('x'); op.create_table(")
    world.add_script("sqm_orders", "0012_more", "0011_ship", broken)
    data = world.data()

    with pytest.raises(MigrationError, match="0012_more"):
        world.manager(world.orders).upgrade("sqm_orders")
    assert world.data() == data
    done = {r[1] for r in world.rows()}
    assert {"0007_base", "0011_ship"} <= done and "0012_more" not in done

    world.add_script("sqm_orders", "0012_more", "0011_ship")
    assert world.manager(world.orders).upgrade("sqm_orders") == [TAIL]
    assert world.data() == {**data, "0012_more": []}
    assert {r[1] for r in world.rows()} == {"0007_base", "0010_finalize", "0011_ship", "0012_more"}


def test_ensure_schema_reconciles_then_reports_upgraded(world: World) -> None:
    world.seed(_old_orders())
    world.business(OLD_ORDERS)
    world.add_script("sqm_orders", "0012_more", "0011_ship")
    data = world.data()
    manager = world.manager(world.orders)
    assert manager.ensure_schema("sqm_orders") == "upgraded"
    assert world.data() == {**data, "0012_more": []}
    assert {r[1] for r in world.rows()} == {"0007_base", "0010_finalize", "0011_ship", "0012_more"}
    assert manager.ensure_schema("sqm_orders") == "up-to-date"


def test_reconcile_without_new_migration_rewrites_only_the_ledger(world: World) -> None:
    world.seed(_old_orders())
    world.business(OLD_ORDERS)
    data = world.data()
    world.manager(world.orders).ensure_schema("sqm_orders")
    assert world.data() == data
    assert {r[1] for r in world.rows()} == {"0007_base", "0010_finalize", "0011_ship"}


def test_fresh_database_executes_the_squashed_script_only(world: World) -> None:
    assert world.manager(world.orders).ensure_schema("sqm_orders") == "created"
    assert world.tables() == {"bedrock_migrations", "0007_base", "0010_finalize", "0011_ship"}
    assert {r[1] for r in world.rows()} == {"0007_base", "0010_finalize", "0011_ship"}


def test_current_ledger_is_skipped_without_any_squash_write(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    o = "sqm_orders"
    world.seed([(o, "0007_base", (), "executed"), (*END, ((o, "0007_base"),), "executed"), (*SHIP, (END,), "executed")])
    before = world.rows()
    calls: list[object] = []
    monkeypatch.setattr(MigrationLedger, "replace_squash", lambda *a: calls.append(a))
    assert world.manager(world.orders).ensure_schema(o) == "up-to-date"
    assert (calls, world.rows()) == ([], before)


@pytest.mark.parametrize("call", ["upgrade", "ensure_schema"])
@pytest.mark.parametrize(
    ("rows", "message"),
    [
        (_old_orders(3), "0010_finalize"),  # partial: stopped at 0009_fill
        ([(a, r, p, "baseline" if r == "0009_fill" else o) for a, r, p, o in _old_orders()], "baseline"),  # conflict
    ],
)
def test_partial_and_conflicting_ledgers_are_refused_untouched(
    world: World, call: str, rows: list[tuple], message: str
) -> None:
    world.seed(rows)
    before, tables = world.rows(), world.tables()
    with pytest.raises(MigrationError, match=message):
        getattr(world.manager(world.orders), call)("sqm_orders")
    assert (world.rows(), world.tables()) == (before, tables)


def test_read_only_commands_never_mutate_an_old_ledger(world: World) -> None:
    world.seed(_old_orders())
    world.business(OLD_ORDERS)
    before, data = world.rows(), world.data()
    manager = world.manager(world.orders)
    for command in (manager.current, manager.check, manager.get_history, manager.heads):
        with suppress(MigrationError):  # whether an old ledger is readable is not the contract; mutation is
            command("sqm_orders")
    manager.apply_squash("sqm_orders", "0010_finalize")
    assert (world.rows(), world.data()) == (before, data)


def test_read_only_commands_never_create_a_database(world: World) -> None:
    manager = world.manager(world.orders)
    for command in (manager.current, manager.check, manager.get_history, manager.heads):
        with suppress(MigrationError):
            command("sqm_orders")
    assert not world.db.exists()


def test_upgrade_reconciles_only_ranges_in_the_target_ancestry(world: World) -> None:
    world.seed([*_old_orders(), *_old_billing()])
    billing = [r for r in world.rows() if r[0] == "sqm_billing"]
    manager = world.manager()

    manager.upgrade("sqm_orders")
    assert {r[1] for r in world.rows() if r[0] == "sqm_orders"} == {"0007_base", "0010_finalize", "0011_ship"}
    assert [r for r in world.rows() if r[0] == "sqm_billing"] == billing

    manager.upgrade("sqm_billing")
    assert {r[1] for r in world.rows() if r[0] == "sqm_billing"} == {"0001_base", "0004_c"}


@pytest.mark.parametrize("target", ["head", "+1"])
def test_upgrade_ignores_partial_range_of_unrelated_loaded_module(world: World, target: str) -> None:
    world.seed([*_old_orders(), *_old_billing()[:2]])
    world.add_script(world.orders, "0012_new", "0011_ship")
    billing = [row for row in world.rows() if row[0] == world.billing]
    assert world.manager().upgrade(world.orders, target) == [(world.orders, "0012_new")]
    assert [row for row in world.rows() if row[0] == world.billing] == billing


def test_disjoint_ranges_in_one_ancestry_reconcile_together(world: World) -> None:
    report = _report(world)
    world.seed([*_old_orders(), *_old_billing()])
    assert world.manager(world.orders, world.billing, report).upgrade(report) == [(report, "0001_r")]
    assert {r[1] for r in world.rows() if r[0] == "sqm_orders"} == {"0007_base", "0010_finalize", "0011_ship"}
    assert {r[1] for r in world.rows() if r[0] == "sqm_billing"} == {"0001_base", "0004_c"}


def test_one_blocked_range_leaves_every_range_untouched(world: World) -> None:
    report = _report(world)
    world.seed([*_old_orders(), *_old_billing()[:3]])  # billing stopped before its squash end
    before, tables = world.rows(), world.tables()
    with pytest.raises(MigrationError, match="sqm_billing"):
        world.manager(world.orders, world.billing, report).upgrade(report)
    assert (world.rows(), world.tables()) == (before, tables)


def test_failure_while_reconciling_rolls_back_earlier_ranges(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    report = _report(world)
    world.seed([*_old_orders(), *_old_billing()])
    before, tables = world.rows(), world.tables()
    real, calls = MigrationLedger.replace_squash, []

    def flaky(self, conn, spec):
        calls.append(spec.end)
        if len(calls) == 2:
            raise RuntimeError("second range fails")
        real(self, conn, spec)

    monkeypatch.setattr(MigrationLedger, "replace_squash", flaky)
    with pytest.raises(Exception, match="second range fails"):
        world.manager(world.orders, world.billing, report).upgrade(report)
    assert len(calls) == 2
    assert (world.rows(), world.tables()) == (before, tables)


def test_fresh_database_creates_nothing(world: World) -> None:
    for apply in (False, True):
        report = world.manager(world.orders).apply_squash("sqm_orders", "0010_finalize", apply=apply)
        assert (report.state, report.applied) == ("fresh", False)
        assert not world.db.exists()  # a mistyped SQLite path must not be created


def test_unsupported_dialect_is_rejected_before_connecting(world: World) -> None:
    registry = ModuleRegistry()
    registry.populate([world.orders])
    manager = MigrationsManager(registry, "oracle+oracledb://u:p@h/x")
    for apply in (False, True):
        with pytest.raises(MigrationError, match="not supported for database dialect 'oracle'"):
            manager.apply_squash("sqm_orders", "0010_finalize", apply=apply)


def test_initial_connection_failure_is_translated(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.seed(_old_orders())

    def refuse(self):
        raise sa.exc.OperationalError("connect", {}, Exception("refused"))

    monkeypatch.setattr(sa.engine.Engine, "connect", refuse)
    for apply in (False, True):
        with pytest.raises(MigrationError, match="refused"):
            world.manager(world.orders).apply_squash("sqm_orders", "0010_finalize", apply=apply)


def test_preview_rejects_non_innodb_ledger_before_any_write(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.seed(_old_orders())
    monkeypatch.setattr(MigrationsManager, "_backend", lambda self: "mysql")
    monkeypatch.setattr(MigrationsManager, "_ledger_storage_engine", lambda self, conn: "MyISAM")
    with pytest.raises(MigrationError, match="InnoDB"):
        world.manager(world.orders).apply_squash("sqm_orders", "0010_finalize")


def test_blocked_states_write_nothing(world: World) -> None:
    world.seed(_old_orders(3))  # stopped at 0009_fill
    before = world.rows()
    for apply in (False, True):
        with pytest.raises(MigrationError, match="0010_finalize"):
            world.manager(world.orders).apply_squash("sqm_orders", "0010_finalize", apply=apply)
    assert world.rows() == before


def test_baseline_rows_are_protected(world: World) -> None:
    world.seed([(a, r, p, "baseline" if r == "0009_fill" else o) for a, r, p, o in _old_orders()])
    before = world.rows()
    with pytest.raises(MigrationError, match="baseline"):
        world.manager(world.orders).apply_squash("sqm_orders", "0010_finalize", apply=True)
    assert world.rows() == before


def test_unknown_revision_is_rejected(world: World) -> None:
    with pytest.raises(MigrationError, match="not the end of an installed squash"):
        world.manager(world.orders).apply_squash("sqm_orders", "0011_ship")


def test_two_ranges_one_at_a_time_with_full_preview(world: World) -> None:
    world.seed([*_old_orders(), *_old_billing()])
    manager = world.manager()
    preview = manager.apply_squash("sqm_orders", "0010_finalize")
    assert preview.states == {END: "replace", BILL_END: "replace"}
    assert preview.remaining == (BILL_END,)

    done = manager.apply_squash("sqm_orders", "0010_finalize", apply=True)
    assert done.state == "replace" and done.remaining == (BILL_END,)
    assert {r[1] for r in world.rows() if r[0] == "sqm_billing"} == {"0001_base", "0002_a", "0003_b", "0004_c"}
    mid = manager.apply_squash("sqm_billing", "0004_c")
    assert mid.states == {END: "current", BILL_END: "replace"} and mid.remaining == ()

    manager.apply_squash("sqm_billing", "0004_c", apply=True)
    assert {r[1] for r in world.rows() if r[0] == "sqm_billing"} == {"0001_base", "0004_c"}


def test_other_order_of_two_ranges(world: World) -> None:
    world.seed([*_old_orders(), *_old_billing()])
    manager = world.manager()
    manager.apply_squash("sqm_billing", "0004_c", apply=True)
    assert manager.apply_squash("sqm_orders", "0010_finalize").states == {END: "replace", BILL_END: "current"}


def test_custom_ledger_never_touches_the_default_table(world: World) -> None:
    world.seed(_old_orders(), table="tenant_ledger")
    world.seed(_old_orders())
    default = world.rows()
    manager = world.manager(world.orders, version_table="tenant_ledger")
    assert manager.apply_squash("sqm_orders", "0010_finalize", apply=True).version_table == "tenant_ledger"
    assert world.rows() == default
    assert len(world.rows("tenant_ledger")) == 3


def test_sqlite_write_waits_for_the_write_lock_and_times_out(world: World) -> None:
    world.seed(_old_orders())
    before = world.rows()
    holder = sqlite3.connect(world.db, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        manager = world.manager(world.orders, timeout=0)
        # Preview only reads, so another writer does not block it.
        assert manager.apply_squash("sqm_orders", "0010_finalize").state == "replace"
        with pytest.raises(MigrationError, match="SQLite write lock"):
            manager.apply_squash("sqm_orders", "0010_finalize", apply=True)
        assert world.rows() == before
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert world.manager(world.orders).apply_squash("sqm_orders", "0010_finalize", apply=True).applied


def test_concurrent_applies_change_the_ledger_once(world: World) -> None:
    world.seed(_old_orders())
    results: list[SquashReport] = []
    errors: list[Exception] = []

    managers = [world.manager(world.orders, timeout=10) for _ in range(4)]

    def run(manager: MigrationsManager) -> None:
        try:
            results.append(manager.apply_squash("sqm_orders", "0010_finalize", apply=True))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(m,)) for m in managers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sorted(r.applied for r in results) == [False, False, False, True]
    assert {r.state for r in results if not r.applied} == {"current"}
    assert len(world.rows()) == 3


class Trace:
    """Records manager steps with the identity of the connection each used."""

    def __init__(self, world: World, monkeypatch: pytest.MonkeyPatch, backend: str = "postgresql") -> None:
        self.events: list[tuple[str, int]] = []
        self.world = world
        self.fail_commit = False
        self.fail_after_dml = False
        trace = self
        real_end = MigrationsManager._squash_end
        real_read = MigrationLedger.read_applied
        real_replace = MigrationLedger.replace_squash

        def acquire(self, conn, *_):
            trace.events.append(("acquire", id(conn)))

        monkeypatch.setattr(MigrationsManager, "_backend", lambda self: backend)
        monkeypatch.setattr(MigrationsManager, "_ledger_storage_engine", lambda self, conn: "InnoDB")
        monkeypatch.setattr(MigrationsManager, "_acquire_dialect_lock", acquire)
        monkeypatch.setattr(MigrationsManager, "_acquire_postgresql_xact_lock", acquire)
        monkeypatch.setattr(
            MigrationsManager, "_release_dialect_lock", lambda self, conn, d: trace.events.append(("release", id(conn)))
        )

        def read(self, conn):
            trace.events.append(("read", id(conn)))
            return real_read(self, conn)

        def replace(self, conn, spec):
            real_replace(self, conn, spec)
            trace.events.append(("replace", id(conn)))
            if trace.fail_after_dml:
                raise RuntimeError("after dml")

        def end(conn, backend, *, commit):
            if commit:
                real_end(conn, backend, commit=True)
                trace.events.append(("commit", id(conn)))
                if trace.fail_commit:
                    raise sa.exc.OperationalError("COMMIT", {}, Exception("connection lost"))
                return
            trace.events.append(("rollback", id(conn)))
            real_end(conn, backend, commit=False)

        monkeypatch.setattr(MigrationLedger, "read_applied", read)
        monkeypatch.setattr(MigrationLedger, "replace_squash", replace)
        monkeypatch.setattr(MigrationsManager, "_squash_end", staticmethod(end))

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.events]

    @property
    def connections(self) -> set[int]:
        return {cid for _, cid in self.events}


# PostgreSQL's transaction-scoped lock ends with commit/rollback; MySQL's session lock needs an explicit release.
BACKENDS = [("postgresql", ""), ("mysql", "release")]


@pytest.mark.parametrize(("backend", "release"), BACKENDS)
def test_network_dialect_locks_reads_writes_commits_on_one_connection(
    world: World, monkeypatch: pytest.MonkeyPatch, backend: str, release: str
) -> None:
    world.seed(_old_orders())
    trace = Trace(world, monkeypatch, backend)
    assert world.manager(world.orders).apply_squash("sqm_orders", "0010_finalize", apply=True).applied
    assert trace.names == ["acquire", "read", "replace", "commit", *filter(None, [release])]
    assert len(trace.connections) == 1


@pytest.mark.parametrize(("backend", "release"), BACKENDS)
def test_lock_is_held_through_rollback_after_a_failed_write(
    world: World, monkeypatch: pytest.MonkeyPatch, backend: str, release: str
) -> None:
    world.seed(_old_orders())
    before = world.rows()
    trace = Trace(world, monkeypatch, backend)
    trace.fail_after_dml = True
    with pytest.raises(RuntimeError, match="after dml"):
        world.manager(world.orders).apply_squash("sqm_orders", "0010_finalize", apply=True)
    assert trace.names == ["acquire", "read", "replace", "rollback", *filter(None, [release])]
    assert len(trace.connections) == 1
    assert world.rows() == before


@pytest.mark.parametrize(("backend", "release"), BACKENDS)
def test_lost_commit_result_fails_then_reconnect_classifies_current(
    world: World, monkeypatch: pytest.MonkeyPatch, backend: str, release: str
) -> None:
    world.seed(_old_orders())
    trace = Trace(world, monkeypatch, backend)
    trace.fail_commit = True
    with pytest.raises(MigrationError, match="Reconnect and preview"):
        world.manager(world.orders).apply_squash("sqm_orders", "0010_finalize", apply=True)
    tail = ["commit", "rollback", *filter(None, [release])]
    assert trace.names[-len(tail) :] == tail
    committed = world.rows()

    trace.fail_commit = False
    trace.events.clear()
    again = world.manager(world.orders).apply_squash("sqm_orders", "0010_finalize", apply=True)
    assert (again.state, again.applied) == ("current", False)
    assert "replace" not in trace.names and world.rows() == committed


@pytest.mark.parametrize(("backend", "release"), BACKENDS)
def test_lock_timeout_stops_before_any_read(
    world: World, monkeypatch: pytest.MonkeyPatch, backend: str, release: str
) -> None:
    world.seed(_old_orders())
    trace = Trace(world, monkeypatch, backend)

    def busy(self, conn, *_):
        raise MigrationError("Timed out waiting for the migration lock.")

    monkeypatch.setattr(MigrationsManager, "_acquire_dialect_lock", busy)
    monkeypatch.setattr(MigrationsManager, "_acquire_postgresql_xact_lock", busy)
    with pytest.raises(MigrationError, match="migration lock"):
        world.manager(world.orders).apply_squash("sqm_orders", "0010_finalize", apply=True)
    assert "read" not in trace.names and "release" not in trace.names


@pytest.mark.parametrize("backend", ["postgresql", "mysql"])
def test_driver_autocommit_is_rejected_before_lock_and_write(
    world: World, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    world.seed(_old_orders())
    before = world.rows()
    trace = Trace(world, monkeypatch, backend)
    monkeypatch.setattr(MigrationsManager, "_autocommit_hazard", staticmethod(lambda conn: True))
    with pytest.raises(MigrationError, match="autocommit"):
        world.manager(world.orders).apply_squash("sqm_orders", "0010_finalize", apply=True)
    assert not {"acquire", "read", "replace", "commit", "release"} & set(trace.names)
    assert world.rows() == before


def test_autocommit_hazard_detects_isolation_level_and_driver_flags() -> None:
    def conn(level: str, raw: object) -> object:
        return type(
            "C",
            (),
            {"get_isolation_level": lambda self: level, "connection": type("K", (), {"dbapi_connection": raw})()},
        )()

    hazard = MigrationsManager._autocommit_hazard
    plain = type("R", (), {"autocommit": False})()
    assert hazard(conn("AUTOCOMMIT", plain))  # type: ignore[arg-type]
    assert hazard(conn("READ COMMITTED", type("R", (), {"autocommit": True})()))  # type: ignore[arg-type]
    assert hazard(conn("READ COMMITTED", type("R", (), {"get_autocommit": lambda self: True})()))  # type: ignore[arg-type]
    assert not hazard(conn("READ COMMITTED", plain))  # type: ignore[arg-type]
    assert not hazard(conn("READ COMMITTED", type("R", (), {"get_autocommit": lambda self: False})()))  # type: ignore[arg-type]


def test_postgresql_lock_is_transaction_scoped_and_times_out(world: World) -> None:
    sql: list[str] = []

    class Conn:
        def __init__(self, answers: list[bool]) -> None:
            self.answers = answers

        def execute(self, stmt, params):
            sql.append(str(stmt))
            return type("Res", (), {"scalar": lambda _, a=self.answers.pop(0): a})()

    manager = world.manager(world.orders, timeout=1)
    manager._acquire_postgresql_xact_lock(Conn([False, True]))  # type: ignore[arg-type]
    assert sql == ["SELECT pg_try_advisory_xact_lock(:key)"] * 2
    with pytest.raises(MigrationError, match="PostgreSQL migration lock"):
        world.manager(world.orders, timeout=0)._acquire_postgresql_xact_lock(Conn([False]))  # type: ignore[arg-type]


def test_mysql_requires_innodb_for_the_existing_ledger(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.seed(_old_orders())
    before = world.rows()
    trace = Trace(world, monkeypatch, backend="mysql")
    monkeypatch.setattr(MigrationsManager, "_ledger_storage_engine", lambda self, conn: "MyISAM")
    with pytest.raises(MigrationError, match="InnoDB"):
        world.manager(world.orders).apply_squash("sqm_orders", "0010_finalize", apply=True)
    assert trace.names == ["acquire", "read", "rollback", "release"] and world.rows() == before

    monkeypatch.setattr(MigrationsManager, "_ledger_storage_engine", lambda self, conn: "InnoDB")
    assert world.manager(world.orders).apply_squash("sqm_orders", "0010_finalize", apply=True).applied


def test_prepare_squash_delegates_with_the_registry(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, object] = {}

    def fake(registry, app, first, through, *, out, script=None, rename=None):
        seen.update(registry=registry, args=(app, first, through), out=out, script=script, rename=rename)
        return out / "x.py"

    monkeypatch.setattr(squash_writer, "prepare_candidate", fake)
    manager = world.manager(world.orders)
    assert manager.prepare_squash("a", "b", "c", out=tmp_path, rename="r") == tmp_path / "x.py"
    assert seen["args"] == ("a", "b", "c") and seen["rename"] == "r" and seen["script"] is None
    assert seen["registry"] is manager._registry
