from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from bedrock.database.migration.graph import MigrationError, MigrationKey
from bedrock.database.migration.ledger import AppliedMigration, MigrationLedger, _encode, project_squashes
from bedrock.database.migration.squash import SquashSpec

L = MigrationLedger("bedrock_migrations")


def o(rev: str) -> MigrationKey:
    return ("orders", rev)


U1 = ("users", "0001")
OLD = (
    (o("0008_add"), (o("0007_base"), U1)),
    (o("0009_fill"), (o("0008_add"),)),
    (o("0010_finalize"), (o("0009_fill"),)),
)
SPEC = SquashSpec("orders", OLD, o("0010_finalize"), (o("0007_base"), U1))
RENAMED = SquashSpec("orders", OLD, o("0002_feature"), (o("0007_base"), U1))
# A second, independent range of another module.
BILLING_OLD = (
    (("billing", "b2"), (("billing", "b1"),)),
    (("billing", "b3"), (("billing", "b2"),)),
)
BILLING = SquashSpec("billing", BILLING_OLD, ("billing", "b3"), (("billing", "b1"),))

FULL: list[tuple[MigrationKey, tuple[MigrationKey, ...], str]] = [
    (U1, (), "executed"),
    (o("0007_base"), (), "executed"),
    *((k, p, "executed") for k, p in OLD),
    (o("0011_ship"), (o("0010_finalize"),), "executed"),
]


@pytest.fixture
def engine(tmp_path: Path):
    eng = sa.create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    yield eng
    eng.dispose()


def seed(engine: sa.Engine, rows, led: MigrationLedger = L) -> None:
    with engine.begin() as conn:
        led.ensure_table(conn)
        for day, (key, parents, origin) in enumerate(rows, start=1):
            conn.execute(
                sa.insert(led._table).values(
                    app=key[0],
                    revision=key[1],
                    parents=_enc(parents),
                    origin=origin,
                    applied_at=datetime(2026, 1, day, tzinfo=UTC),
                )
            )


_enc = _encode


def raw(engine: sa.Engine, led: MigrationLedger = L) -> list[tuple]:
    """Every column of every row, including timestamps."""
    t = led._table
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(sa.select(t).order_by(t.c.app, t.c.revision))]


def applied(engine: sa.Engine, led: MigrationLedger = L) -> dict[MigrationKey, AppliedMigration]:
    with engine.connect() as conn:
        return led.read_applied(conn)


def replace(engine: sa.Engine, spec: SquashSpec, led: MigrationLedger = L) -> None:
    with engine.begin() as conn:
        led.replace_squash(conn, spec)


def test_fresh_writes_nothing_and_creates_no_table(engine: sa.Engine) -> None:
    assert applied(engine) == {}
    states, projected = project_squashes({}, {SPEC.end: SPEC})
    assert (states, projected) == ({SPEC.end: "fresh"}, {})
    assert not sa.inspect(engine).has_table(L.name)


def test_fresh_with_unrelated_rows_is_unchanged(engine: sa.Engine) -> None:
    seed(engine, FULL[:2])
    rows = applied(engine)
    states, projected = project_squashes(rows, {SPEC.end: SPEC})
    assert states == {SPEC.end: "fresh"} and projected == rows


def test_replace_projects_new_end_and_keeps_successors(engine: sa.Engine) -> None:
    seed(engine, FULL)
    rows = applied(engine)
    states, projected = project_squashes(rows, {SPEC.end: SPEC})
    assert states == {SPEC.end: "replace"}
    assert set(projected) == set(rows) - {o("0008_add"), o("0009_fill")}
    assert projected[SPEC.end] == AppliedMigration("orders", "0010_finalize", SPEC.parents, "executed")
    assert projected[o("0011_ship")] == rows[o("0011_ship")]
    assert rows[o("0009_fill")].parents == (o("0008_add"),)  # input is never mutated


def test_replace_updates_one_row_deletes_others_and_keeps_timestamps(engine: sa.Engine) -> None:
    seed(engine, FULL)
    before = {r[:2]: r for r in raw(engine)}
    replace(engine, SPEC)
    after = {r[:2]: r for r in raw(engine)}

    assert set(after) == set(before) - {("orders", "0008_add"), ("orders", "0009_fill")}
    end = after[("orders", "0010_finalize")]
    assert end[2] == _enc(SPEC.parents)
    assert (end[3], end[4]) == (before[("orders", "0010_finalize")][3], before[("orders", "0010_finalize")][4])
    for key, row in after.items():
        if key != ("orders", "0010_finalize"):
            assert row == before[key]
    states, projected = project_squashes(applied(engine), {SPEC.end: SPEC})
    assert states == {SPEC.end: "current"} and projected == applied(engine)


def test_current_is_recognised_without_any_write(engine: sa.Engine) -> None:
    seed(engine, FULL)
    replace(engine, SPEC)
    statements: list[str] = []
    sa.event.listen(engine, "before_cursor_execute", lambda c, cur, stmt, *a: statements.append(stmt))
    states, _ = project_squashes(applied(engine), {SPEC.end: SPEC})
    assert states == {SPEC.end: "current"}
    assert all(s.lstrip().upper().startswith(("SELECT", "PRAGMA")) for s in statements)


def test_current_from_a_database_that_ran_the_new_script(engine: sa.Engine) -> None:
    seed(engine, [FULL[0], FULL[1], (SPEC.end, SPEC.parents, "executed"), FULL[-1]])
    states, _ = project_squashes(applied(engine), {SPEC.end: SPEC})
    assert states == {SPEC.end: "current"}


def test_current_accepts_baseline_origin(engine: sa.Engine) -> None:
    seed(engine, [(U1, (), "baseline"), (o("0007_base"), (), "baseline"), (SPEC.end, SPEC.parents, "baseline")])
    assert project_squashes(applied(engine), {SPEC.end: SPEC})[0] == {SPEC.end: "current"}


def _without(*revisions: str):
    return [r for r in FULL if r[0] not in {o(x) for x in revisions}]


def _with(key: MigrationKey, **changes):
    def patch(row):
        if row[0] != key:
            return row
        return (row[0], changes.get("parents", row[1]), changes.get("origin", row[2]))

    return [patch(r) for r in FULL]


BLOCKED = {
    "partial-tail": (_without("0010_finalize", "0011_ship"), "Upgrade with the previous release"),
    "partial-middle": (_without("0009_fill"), "Upgrade with the previous release"),
    "end-only-old-parents": (_without("0008_add", "0009_fill"), "Upgrade with the previous release"),
    "end-new-parents-with-old-rows": (
        _with(o("0010_finalize"), parents=SPEC.parents),
        "parents drifted",
    ),
    "baseline-row": (_with(o("0009_fill"), origin="baseline"), "baseline rows are protected"),
    "parent-drift": (_with(o("0009_fill"), parents=(o("0007_base"),)), "parents drifted"),
    "external-parent-missing": ([r for r in FULL if r[0] != U1], "not applied"),
    "successor-without-end": (_without("0008_add", "0009_fill", "0010_finalize"), "orders:0010_finalize"),
    "unloaded-reference": (
        [*FULL, (("ghost", "g1"), (o("0009_fill"),), "executed")],
        "retired parents orders:0009_fill",
    ),
}


@pytest.mark.parametrize("name", BLOCKED)
def test_blocked_states_raise_and_projection_is_all_or_nothing(engine: sa.Engine, name: str) -> None:
    rows, message = BLOCKED[name]
    seed(engine, rows)
    before = raw(engine)
    with pytest.raises(MigrationError, match=message):
        project_squashes(applied(engine), {SPEC.end: SPEC})
    assert raw(engine) == before


def test_blocked_partial_message_names_the_old_end_not_a_renamed_end(engine: sa.Engine) -> None:
    seed(engine, _without("0010_finalize", "0011_ship"))
    with pytest.raises(MigrationError, match="orders:0010_finalize"):
        project_squashes(applied(engine), {RENAMED.end: RENAMED})


def test_rename_replaces_old_end_with_new_key_and_keeps_origin_and_time(engine: sa.Engine) -> None:
    seed(engine, _without("0011_ship"))
    before = {r[:2]: r for r in raw(engine)}
    states, projected = project_squashes(applied(engine), {RENAMED.end: RENAMED})
    assert states == {RENAMED.end: "replace"}
    assert set(projected) == {U1, o("0007_base"), RENAMED.end}

    replace(engine, RENAMED)
    after = {r[:2]: r for r in raw(engine)}
    assert set(after) == {("users", "0001"), ("orders", "0007_base"), ("orders", "0002_feature")}
    old_end = before[("orders", "0010_finalize")]
    assert after[("orders", "0002_feature")] == ("orders", "0002_feature", _enc(RENAMED.parents), *old_end[3:])
    assert project_squashes(applied(engine), {RENAMED.end: RENAMED})[0] == {RENAMED.end: "current"}


def test_rename_blocks_references_to_old_end_and_mixed_keys(engine: sa.Engine) -> None:
    seed(engine, FULL)  # 0011_ship still points at the old end 0010_finalize
    with pytest.raises(MigrationError, match="retired parents orders:0010_finalize"):
        project_squashes(applied(engine), {RENAMED.end: RENAMED})
    mixed = [*_without("0011_ship"), (RENAMED.end, RENAMED.parents, "executed")]
    engine2 = sa.create_engine("sqlite://")
    seed(engine2, mixed)
    with pytest.raises(MigrationError, match="Upgrade with the previous release"):
        project_squashes(applied(engine2), {RENAMED.end: RENAMED})


def test_two_ranges_project_together_and_switch_one_at_a_time(engine: sa.Engine) -> None:
    b = [
        (("billing", "b1"), (), "executed"),
        *((k, p, "executed") for k, p in BILLING_OLD),
        (("billing", "b4"), (("billing", "b3"),), "executed"),
    ]
    seed(engine, [*FULL, *b])
    specs = {SPEC.end: SPEC, BILLING.end: BILLING}
    states, projected = project_squashes(applied(engine), specs)
    assert states == {SPEC.end: "replace", BILLING.end: "replace"}
    assert ("billing", "b2") not in projected and o("0008_add") not in projected

    replace(engine, BILLING)
    states, projected = project_squashes(applied(engine), specs)
    assert states == {SPEC.end: "replace", BILLING.end: "current"}
    replace(engine, SPEC)
    assert set(project_squashes(applied(engine), specs)[0].values()) == {"current"}


def test_one_blocked_range_blocks_the_whole_projection(engine: sa.Engine) -> None:
    seed(engine, [*FULL, (("billing", "b1"), (), "executed"), (("billing", "b3"), (("billing", "b2"),), "executed")])
    with pytest.raises(MigrationError, match="billing:b3"):
        project_squashes(applied(engine), {SPEC.end: SPEC, BILLING.end: BILLING})


def test_phantom_new_endpoint_reference_is_rejected_across_ranges(engine: sa.Engine) -> None:
    # billing:b4 names RENAMED.end, which no applied row has; projecting the orders rename must not make it real.
    b = [(("billing", "b1"), (), "executed"), *((k, p, "executed") for k, p in BILLING_OLD)]
    seed(engine, [*_without("0011_ship"), *b, (("billing", "b4"), (RENAMED.end,), "executed")])
    before = raw(engine)
    with pytest.raises(MigrationError, match="unrecorded orders:0002_feature"):
        project_squashes(applied(engine), {RENAMED.end: RENAMED, BILLING.end: BILLING})
    assert raw(engine) == before


@pytest.mark.parametrize(
    "body",
    [
        "UPDATE bedrock_migrations SET origin = 'baseline' WHERE app = 'users';",
        "UPDATE bedrock_migrations SET applied_at = '2000-01-01 00:00:00.000000' WHERE revision = '0010_finalize';",
        "DELETE FROM bedrock_migrations WHERE revision = '0011_ship';",
    ],
)
def test_trigger_mutation_beyond_the_switch_raises_before_commit(engine: sa.Engine, body: str) -> None:
    seed(engine, FULL)
    before = raw(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql(f"CREATE TRIGGER mutate AFTER UPDATE ON bedrock_migrations BEGIN {body} END")
    with pytest.raises(MigrationError, match="beyond the expected switch"):
        replace(engine, SPEC)
    assert raw(engine) == before  # the failed switch was rolled back with the caller's transaction


def test_failure_between_update_and_delete_rolls_back_every_row(engine: sa.Engine) -> None:
    seed(engine, FULL)
    before = raw(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TRIGGER stop BEFORE DELETE ON bedrock_migrations BEGIN SELECT RAISE(ABORT, 'boom'); END"
        )
    with pytest.raises(sa.exc.DatabaseError, match="boom"):
        replace(engine, SPEC)
    assert raw(engine) == before  # UPDATE was undone, timestamps included
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TRIGGER stop")
    replace(engine, SPEC)  # same ledger still applies cleanly
    assert project_squashes(applied(engine), {SPEC.end: SPEC})[0] == {SPEC.end: "current"}


def test_commit_then_lost_client_only_sees_current_and_repeat_writes_nothing(engine: sa.Engine) -> None:
    seed(engine, FULL)
    with pytest.raises(ConnectionError):
        with engine.connect() as conn:
            L.replace_squash(conn, SPEC)
            conn.commit()  # server committed; the client then never sees the result
            raise ConnectionError("lost")
    committed = raw(engine)
    fresh = sa.create_engine(engine.url)  # reconnect
    try:
        states, _ = project_squashes(applied(fresh), {SPEC.end: SPEC})
        assert states == {SPEC.end: "current"}
        assert raw(fresh) == committed
    finally:
        fresh.dispose()


def test_update_row_count_mismatch_is_an_error(engine: sa.Engine) -> None:
    seed(engine, _without("0010_finalize", "0011_ship"))
    with pytest.raises(MigrationError, match="update one row"):
        with engine.begin() as conn:
            L.replace_squash(conn, SPEC)


def test_custom_ledger_is_the_only_table_written(engine: sa.Engine) -> None:
    custom = MigrationLedger("tenant_ledger")
    seed(engine, FULL, custom)
    seed(engine, FULL)
    default_before = raw(engine)
    replace(engine, SPEC, custom)
    assert raw(engine) == default_before
    assert {r[1] for r in raw(engine, custom)} == {r[1] for r in default_before} - {"0008_add", "0009_fill"}


def test_replace_does_not_create_a_missing_table(engine: sa.Engine) -> None:
    with pytest.raises(sa.exc.OperationalError):
        replace(engine, SPEC)
    assert not sa.inspect(engine).has_table(L.name)
