"""End-to-end ``bedrock db squash prepare|apply`` behaviour, driven through fresh CLI processes.

Every database lives in a SQLite file. An old chain, the published replacement script and a database
switched from the old chain are compared on structure, data and the ledger. Output wording is not asserted.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from bedrock.testing.modules import ModuleFactory

_ENTRY = "from bedrock.cli.main import entrypoint; entrypoint()"
_LEDGER = "SELECT app, revision, parents, origin, applied_at FROM bedrock_migrations ORDER BY app, revision"
_APP = "sq_orders"
_OTHER = "sq_other"


def _rev(rev: str, down: str | None, up: str, dn: str) -> str:
    """A script in the shape ``bedrock db revision`` writes."""
    return (
        "from collections.abc import Sequence\n\nimport sqlalchemy as sa\nfrom alembic import op\n\n"
        f"revision: str = {rev!r}\ndown_revision: str | None = {down!r}\ndepends_on: Sequence[str] = ()\n\n\n"
        f"def upgrade() -> None:\n    {up}\n\n\ndef downgrade() -> None:\n    {dn}\n"
    )


_ITEM = "op.create_table('item', sa.Column('id', sa.Integer(), nullable=False), {extra}sa.PrimaryKeyConstraint('id'))"
_SHIP = (
    "op.create_table('shipment', sa.Column('id', sa.Integer(), nullable=False), "
    "sa.Column('item_id', sa.Integer(), nullable=True), sa.PrimaryKeyConstraint('id'))"
)


@dataclass(frozen=True)
class _Chain:
    """Old scripts, the range to squash and the data used on every database."""

    scripts: dict[str, str]
    first: str
    through: str
    seed: str  # at the first supported start (0001_base)
    local: str  # new business data written on top of head
    manual: str | None = None  # hand-written replacement, when the range cannot be generated


def _auto(successor: bool = True) -> _Chain:
    """Structural-only chain: 0002..0004 can be generated; ``successor`` adds 0005 after the range."""
    scripts = {
        "0001_base": _rev(
            "0001_base",
            None,
            _ITEM.format(extra="sa.Column('name', sa.String(length=50), nullable=True), "),
            "op.drop_table('item')",
        ),
        "0002_add": _rev(
            "0002_add",
            "0001_base",
            "op.add_column('item', sa.Column('qty', sa.Integer(), nullable=True))",
            "op.drop_column('item', 'qty')",
        ),
        "0003_idx": _rev(
            "0003_idx",
            "0002_add",
            "op.create_index('ix_item_name', 'item', ['name'], unique=False)",
            "op.drop_index('ix_item_name', table_name='item')",
        ),
        "0004_extra": _rev(
            "0004_extra",
            "0003_idx",
            "op.add_column('item', sa.Column('note', sa.String(length=20), nullable=True))",
            "op.drop_column('item', 'note')",
        ),
    }
    if successor:
        scripts["0005_ship"] = _rev("0005_ship", "0004_extra", _SHIP, "op.drop_table('shipment')")
    return _Chain(
        scripts,
        "0002_add",
        "0004_extra",
        "INSERT INTO item (id, name) VALUES (1, 'a'), (2, 'b');",
        "UPDATE item SET qty = 5, note = 'n' WHERE id = 1;"
        + (" INSERT INTO shipment (id, item_id) VALUES (1, 1);" if successor else ""),
    )


def _manual() -> _Chain:
    """Backfill chain through a temporary column; only a reviewed script can replace it."""
    scripts = {
        "0001_base": _rev(
            "0001_base",
            None,
            _ITEM.format(extra="sa.Column('qty', sa.Integer(), nullable=True), "),
            "op.drop_table('item')",
        ),
        "0002_tmp": _rev(
            "0002_tmp",
            "0001_base",
            "op.add_column('item', sa.Column('tmp_total', sa.Integer(), nullable=True))",
            "op.drop_column('item', 'tmp_total')",
        ),
        "0003_fill": _rev("0003_fill", "0002_tmp", "op.execute('UPDATE item SET tmp_total = qty * 2')", "pass"),
        "0004_final": _rev(
            "0004_final",
            "0003_fill",
            "op.add_column('item', sa.Column('total', sa.Integer(), nullable=True))\n"
            "    op.execute('UPDATE item SET total = tmp_total')\n    op.drop_column('item', 'tmp_total')",
            "op.add_column('item', sa.Column('tmp_total', sa.Integer(), nullable=True))\n"
            "    op.execute('UPDATE item SET tmp_total = total')\n    op.drop_column('item', 'total')",
        ),
        "0005_ship": _rev("0005_ship", "0004_final", _SHIP, "op.drop_table('shipment')"),
    }
    merged = _rev(
        "0004_final",
        "0001_base",
        "op.add_column('item', sa.Column('total', sa.Integer(), nullable=True))\n"
        "    op.execute('UPDATE item SET total = qty * 2')",
        "op.drop_column('item', 'total')",
    )
    return _Chain(
        scripts,
        "0002_tmp",
        "0004_final",
        "INSERT INTO item (id, qty) VALUES (1, 3), (2, 4);",
        "UPDATE item SET qty = 10 WHERE id = 1; INSERT INTO shipment (id, item_id) VALUES (1, 1);",
        merged,
    )


class _Proj:
    """One module, several SQLite files, and the author's replace-the-files step."""

    def __init__(self, factory: ModuleFactory, tmp: Path, chain: _Chain) -> None:
        self.factory, self.tmp, self.chain = factory, tmp, chain
        self.mig = factory.root / _APP / "migrations"
        factory(
            _APP,
            manifest={"title": _APP, "version": "0.1.0"},
            models="",
            files={f"migrations/{rev}.py": src for rev, src in chain.scripts.items()},
        )
        self.out = tmp / "candidate"
        self.out.mkdir()

    @property
    def old_range(self) -> list[str]:
        revs = list(self.chain.scripts)
        return revs[revs.index(self.chain.first) : revs.index(self.chain.through) + 1]

    def cli(self, db: Path | None, *args: str, offline: bool = False) -> subprocess.CompletedProcess[str]:
        env = {k: v for k, v in os.environ.items() if not (offline and k.startswith("DATABASE_"))}
        env["PYTHONPATH"] = os.pathsep.join([str(self.factory.root), *filter(None, [os.environ.get("PYTHONPATH")])])
        env["NO_COLOR"] = "1"
        if db is not None:
            env.update(DATABASE_TYPE="sqlite", DATABASE_DRIVER="pysqlite", DATABASE_SCHEMA=str(db))
        return subprocess.run(
            [sys.executable, "-c", _ENTRY, "db", *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def db(self, name: str) -> Path:
        return self.tmp / f"{name}.db"

    def roots(self, *extra: str) -> list[str]:
        return [a for mod in (_APP, *extra) for a in ("--root", mod)]

    def prepare(
        self, *extra: str, roots: tuple[str, ...] = (), script: bool | None = None
    ) -> subprocess.CompletedProcess[str]:
        args = ["squash", "prepare", _APP, "--from", self.chain.first, "--through", self.chain.through]
        args += [*self.roots(*roots), "--out", str(self.out), *extra]
        if (script if script is not None else self.chain.manual is not None) and self.chain.manual:
            path = self.tmp / "handwritten.py"
            path.write_text(self.chain.manual, encoding="utf-8")
            args += ["--script", str(path)]
        return self.cli(None, *args, offline=True)

    def candidate(self, revision: str | None = None) -> Path:
        return self.out / f"{revision or self.chain.through}.py"

    def apply(
        self, db: Path, revision: str | None = None, *, write: bool = False, roots: tuple[str, ...] = ()
    ) -> subprocess.CompletedProcess[str]:
        args = ["squash", "apply", _APP, revision or self.chain.through, *self.roots(*roots)]
        return self.cli(db, *args, *(["--apply"] if write else []))

    def publish(self, revision: str | None = None) -> None:
        """Replace the old range files with the candidate, as the author does in version control."""
        for rev in self.old_range:
            (self.mig / f"{rev}.py").unlink()
        shutil.copy(self.candidate(revision), self.mig / f"{revision or self.chain.through}.py")

    def restore(self, rev: str) -> None:
        (self.mig / f"{rev}.py").write_text(self.chain.scripts[rev], encoding="utf-8")

    def ok(self, db: Path | None, *args: str) -> subprocess.CompletedProcess[str]:
        proc = self.cli(db, *args)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return proc

    def bring_up(self, db: Path, target: str = "head", *, seed: bool = True, local: bool = True) -> None:
        """base -> seed -> target -> local writes (only when ``target`` is head)."""
        self.ok(db, "upgrade", _APP, "0001_base")
        if seed:
            self.exec(db, self.chain.seed)
        self.ok(db, "upgrade", _APP, target)
        if local and target == "head":
            self.exec(db, self.chain.local)

    @staticmethod
    def exec(db: Path, script: str) -> None:
        with sqlite3.connect(db) as conn:
            conn.executescript(script)

    @staticmethod
    def rows(db: Path, query: str = _LEDGER) -> list[tuple]:
        if not db.exists():
            return []
        with sqlite3.connect(db) as conn:
            return conn.execute(query).fetchall()

    def ledger(self, db: Path) -> list[tuple]:
        return self.rows(db) if "bedrock_migrations" in self.names(db) else []

    @staticmethod
    def names(db: Path) -> set[str]:
        if not db.exists():
            return set()
        with sqlite3.connect(db) as conn:
            return {n for (n,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

    def snapshot(self, db: Path) -> dict[str, object]:
        """Business structure (columns, indexes) and rows of every non-ledger table."""
        out: dict[str, object] = {}
        with sqlite3.connect(db) as conn:
            for table in sorted(self.names(db) - {"bedrock_migrations", "alembic_version"}):
                cols = [row[1:] for row in conn.execute(f"PRAGMA table_info({table})")]
                indexes = sorted(
                    (name, unique, tuple(c[2] for c in conn.execute(f"PRAGMA index_info({name})")))
                    for _, name, unique, *_ in conn.execute(f"PRAGMA index_list({table})")
                )
                out[table] = (cols, indexes, conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall())
        return out


def _no_ts(rows: list[tuple]) -> list[tuple]:
    return [row[:4] for row in rows]


@pytest.fixture(params=["auto", "manual"])
def proj(request: pytest.FixtureRequest, module_factory: ModuleFactory, tmp_path: Path) -> _Proj:
    return _Proj(module_factory, tmp_path, _auto() if request.param == "auto" else _manual())


@pytest.fixture
def auto(module_factory: ModuleFactory, tmp_path: Path) -> _Proj:
    return _Proj(module_factory, tmp_path, _auto())


@pytest.fixture
def tail(module_factory: ModuleFactory, tmp_path: Path) -> _Proj:
    """The squashed range is the end of the chain (no successor), the shape of a feature branch."""
    return _Proj(module_factory, tmp_path, _auto(successor=False))


class TestPrepare:
    def test_roots_and_out_are_required(self, auto: _Proj) -> None:
        base = ["squash", "prepare", _APP, "--from", "0002_add", "--through", "0004_extra"]
        assert auto.cli(None, *base, "--out", str(auto.out), offline=True).returncode != 0
        assert auto.cli(None, *base, *auto.roots(), offline=True).returncode != 0
        assert not list(auto.out.iterdir())

    def test_offline_writes_only_the_candidate(self, proj: _Proj) -> None:
        before = sorted(p.name for p in proj.mig.glob("*.py"))
        done = proj.prepare()
        assert done.returncode == 0, done.stdout + done.stderr
        assert sorted(p.name for p in proj.mig.glob("*.py")) == before

    def test_existing_candidate_is_never_overwritten(self, auto: _Proj) -> None:
        assert auto.prepare().returncode == 0
        first = auto.candidate().read_text(encoding="utf-8")
        auto.candidate().write_text(first + "# reviewed\n", encoding="utf-8")
        assert auto.prepare().returncode != 0
        assert auto.candidate().read_text(encoding="utf-8").endswith("# reviewed\n")

    def test_unsupported_operation_is_refused_without_script(
        self, module_factory: ModuleFactory, tmp_path: Path
    ) -> None:
        chain = _manual()
        proj = _Proj(module_factory, tmp_path, chain)
        assert proj.prepare(script=False).returncode != 0
        assert not list(proj.out.iterdir())


class TestOldCleanSwitched:
    def test_three_databases_agree_and_switch_is_ledger_only(self, proj: _Proj) -> None:
        old, new, switched, auto_db = proj.db("old"), proj.db("new"), proj.db("switched"), proj.db("auto")
        end = proj.chain.through

        proj.bring_up(old)
        proj.bring_up(switched)
        proj.bring_up(auto_db)
        old_head = proj.snapshot(old)
        proj.ok(old, "downgrade", _APP, "0001_base")
        old_base = proj.snapshot(old)
        assert old_head != old_base

        assert proj.prepare().returncode == 0
        proj.publish()

        # Old ledger plus new code: read-only commands never reconcile.
        before = proj.ledger(switched)
        for command in ("check", "current", "history", "heads"):
            proj.cli(switched, command, _APP)
            assert proj.ledger(switched) == before

        # An ordinary upgrade reconciles the complete range itself and replays nothing.
        auto_data = proj.snapshot(auto_db)
        proj.ok(auto_db, "upgrade", _APP)
        assert proj.snapshot(auto_db) == auto_data == old_head

        # A database with no ledger rows: apply writes nothing; the next upgrade runs the script.
        assert proj.apply(new, write=True).returncode == 0
        assert proj.names(new) == set()
        proj.bring_up(new)
        assert proj.snapshot(new) == old_head
        new_rows = proj.ledger(new)
        assert {row[1] for row in new_rows} == {"0001_base", end, "0005_ship"}

        # Preview never writes; confirmation switches rows only.
        data = proj.snapshot(switched)
        preview = proj.apply(switched)
        assert preview.returncode == 0, preview.stdout + preview.stderr
        assert proj.ledger(switched) == before

        done = proj.apply(switched, write=True)
        assert done.returncode == 0, done.stdout + done.stderr
        after = proj.ledger(switched)
        assert proj.snapshot(switched) == data == old_head
        assert _no_ts(after) == _no_ts(new_rows) == _no_ts(proj.ledger(auto_db))
        by_key = {row[:2]: row for row in before}
        for row in after:
            old_row = by_key[row[:2]]
            assert row[3:] == old_row[3:]  # origin and applied_at survive
            if row[1] != end:
                assert row == old_row  # parents of 0001 and the successor stay as they were

        again = proj.apply(switched, write=True)
        assert again.returncode == 0, again.stdout + again.stderr
        assert proj.ledger(switched) == after
        assert proj.ok(switched, "upgrade", _APP).stdout.strip() == "Nothing to do."

        for db in (new, switched):
            proj.ok(db, "downgrade", _APP, "0001_base")
            assert proj.snapshot(db) == old_base
            assert {r[1] for r in proj.ledger(db)} == {"0001_base"}
            proj.ok(db, "upgrade", _APP)
            assert {t: v[:2] for t, v in proj.snapshot(db).items()} == {t: v[:2] for t, v in old_head.items()}


class TestUpgradeReconciles:
    _TAIL = _rev(
        "0006_note",
        "0005_ship",
        "op.create_table('note', sa.Column('id', sa.Integer(), nullable=False), sa.PrimaryKeyConstraint('id'))",
        "op.drop_table('note')",
    )

    def _production(self, auto: _Proj, tail: str | None = None) -> Path:
        """Production at 0005 with the old scripts, then the author publishes the squash and optionally a 0006."""
        db = auto.db("prod")
        auto.bring_up(db)
        assert auto.prepare().returncode == 0
        auto.publish()
        if tail:
            (auto.mig / "0006_note.py").write_text(tail, encoding="utf-8")
        return db

    def test_new_0006_runs_alone_without_replay_and_retry_is_a_no_op(self, auto: _Proj) -> None:
        db = self._production(auto, self._TAIL)
        data = auto.snapshot(db)

        out = auto.ok(db, "upgrade", _APP).stdout
        assert "0006_note" in out and "0003_idx" not in out and "0005_ship" not in out
        assert {r[1] for r in auto.ledger(db)} == {"0001_base", "0004_extra", "0005_ship", "0006_note"}
        assert {t: v for t, v in auto.snapshot(db).items() if t != "note"} == data  # every row preserved

        rows = auto.ledger(db)
        assert auto.ok(db, "upgrade", _APP).stdout.strip() == "Nothing to do."
        assert auto.ledger(db) == rows

    def test_failed_0006_keeps_data_and_the_retry_runs_only_it(self, auto: _Proj) -> None:
        broken = self._TAIL.replace("op.create_table(", "op.execute('SELECT * FROM missing'); op.create_table(")
        db = self._production(auto, broken)
        data = auto.snapshot(db)

        assert auto.cli(db, "upgrade", _APP).returncode != 0
        assert auto.snapshot(db) == data
        assert "0006_note" not in {r[1] for r in auto.ledger(db)}

        (auto.mig / "0006_note.py").write_text(self._TAIL, encoding="utf-8")
        out = auto.ok(db, "upgrade", _APP).stdout
        assert "0006_note" in out and "0003_idx" not in out and "0005_ship" not in out
        assert {t: v for t, v in auto.snapshot(db).items() if t != "note"} == data

    def test_fresh_database_runs_the_published_script_and_0006(self, auto: _Proj) -> None:
        self._production(auto, self._TAIL)
        fresh = auto.db("fresh")
        out = auto.ok(fresh, "upgrade", _APP).stdout
        assert "0004_extra" in out and "0006_note" in out and "0002_add" not in out
        assert {r[1] for r in auto.ledger(fresh)} == {"0001_base", "0004_extra", "0005_ship", "0006_note"}


class TestRejectedStates:
    def test_leftover_retired_script_is_refused_on_every_path(self, proj: _Proj) -> None:
        applied, fresh = proj.db("applied"), proj.db("fresh")
        proj.bring_up(applied)
        assert proj.prepare().returncode == 0
        proj.publish()
        proj.restore(proj.old_range[0])  # a retired script left behind as an orphan branch

        ledger, data = proj.ledger(applied), proj.snapshot(applied)
        assert proj.cli(fresh, "upgrade", _APP).returncode != 0
        for write in (False, True):
            assert proj.apply(applied, write=write).returncode != 0
            assert proj.apply(fresh, write=write).returncode != 0
        assert (proj.ledger(applied), proj.snapshot(applied)) == (ledger, data)
        assert "item" not in proj.names(fresh)

    def test_partially_applied_database_is_refused(self, proj: _Proj) -> None:
        partial = proj.db("partial")
        proj.bring_up(partial, proj.old_range[1], seed=True)
        assert proj.prepare().returncode == 0
        proj.publish()

        ledger, data = proj.ledger(partial), proj.snapshot(partial)
        for write in (False, True):
            assert proj.apply(partial, write=write).returncode != 0
        assert proj.cli(partial, "upgrade", _APP).returncode != 0
        assert (proj.ledger(partial), proj.snapshot(partial)) == (ledger, data)


class TestReverseReferences:
    @staticmethod
    def _other(proj: _Proj, target: str) -> None:
        proj.factory(
            _OTHER,
            manifest={"title": _OTHER, "version": "0.1.0", "depends_on": [_APP]},
            models="",
            files={
                "migrations/o1.py": _rev("o1", None, "pass", "pass").replace(
                    "depends_on: Sequence[str] = ()", f"depends_on: Sequence[str] = ({_APP + ':' + target!r},)"
                )
            },
        )

    def test_loaded_reference_to_retired_middle_is_refused(self, auto: _Proj) -> None:
        self._other(auto, "0003_idx")
        assert auto.prepare(roots=(_OTHER,)).returncode != 0
        assert not list(auto.out.iterdir())

    def test_reference_to_kept_end_is_allowed(self, auto: _Proj) -> None:
        self._other(auto, "0004_extra")
        assert auto.prepare(roots=(_OTHER,)).returncode == 0

    def test_reference_recorded_only_in_the_target_ledger_blocks_apply(self, auto: _Proj) -> None:
        db = auto.db("target")
        self._other(auto, "0003_idx")
        auto.bring_up(db)
        auto.ok(db, "upgrade", _OTHER)
        assert auto.prepare().returncode == 0  # the other module is not loaded, so not visible here
        auto.publish()

        ledger, data = auto.ledger(db), auto.snapshot(db)
        for write in (False, True):
            assert auto.apply(db, write=write).returncode != 0
        assert (auto.ledger(db), auto.snapshot(db)) == (ledger, data)
        assert auto.cli(db, "upgrade", _APP).returncode != 0
        assert (auto.ledger(db), auto.snapshot(db)) == (ledger, data)


class TestRename:
    def test_default_keeps_the_end_id_and_successor_untouched(self, auto: _Proj) -> None:
        db = auto.db("db")
        auto.bring_up(db)
        before = auto.ledger(db)
        assert auto.prepare().returncode == 0
        auto.publish()
        assert auto.apply(db, write=True).returncode == 0
        after = {row[:2]: row for row in auto.ledger(db)}
        assert {key[1] for key in after} == {"0001_base", "0004_extra", "0005_ship"}
        assert all(after[row[:2]] == row for row in before if row[1] in {"0001_base", "0005_ship"})

    def test_new_id_runs_for_real_on_a_fresh_database(self, tail: _Proj) -> None:
        assert tail.prepare("--rename", "0002_feature").returncode == 0
        tail.publish("0002_feature")
        fresh, clean = tail.db("fresh"), tail.db("clean")
        tail.bring_up(fresh)
        assert {row[1] for row in tail.ledger(fresh)} == {"0001_base", "0002_feature"}

        # Same structure and data as the old chain ending at 0004_extra.
        for rev in tail.old_range:
            tail.restore(rev)
        (tail.mig / "0002_feature.py").unlink()
        tail.bring_up(clean)
        assert tail.snapshot(fresh) == tail.snapshot(clean)

    def test_switch_is_ledger_only_and_idempotent(self, tail: _Proj) -> None:
        db = tail.db("local")
        tail.bring_up(db)
        before = {row[1]: row for row in tail.ledger(db)}
        data = tail.snapshot(db)
        assert tail.prepare("--rename", "0002_feature").returncode == 0
        tail.publish("0002_feature")

        assert tail.apply(db, "0002_feature").returncode == 0
        assert tail.ledger(db) == list(before.values())  # preview only
        assert tail.apply(db, "0002_feature", write=True).returncode == 0
        after = {row[1]: row for row in tail.ledger(db)}
        assert set(after) == {"0001_base", "0002_feature"}
        assert after["0001_base"] == before["0001_base"]
        assert after["0002_feature"][3:] == before["0004_extra"][3:]  # origin, applied_at
        assert tail.snapshot(db) == data

        rows = tail.ledger(db)
        assert tail.apply(db, "0002_feature", write=True).returncode == 0
        assert tail.ledger(db) == rows
        assert tail.ok(db, "upgrade", _APP).stdout.strip() == "Nothing to do."
        tail.ok(db, "downgrade", _APP, "0001_base")
        assert {row[1] for row in tail.ledger(db)} == {"0001_base"}

    @pytest.mark.parametrize("name", ["0003_idx", "0001_base", "../escape", "0002_feature.py"])
    def test_collisions_and_unsafe_names_are_refused(self, tail: _Proj, name: str) -> None:
        assert tail.prepare("--rename", name).returncode != 0
        assert not list(tail.out.iterdir())
        assert not (tail.tmp / "escape.py").exists()

    def test_successor_pointing_at_old_end_blocks_rename(self, auto: _Proj) -> None:
        assert auto.prepare("--rename", "0002_feature").returncode != 0
        assert not list(auto.out.iterdir())

    def test_other_module_pointing_at_old_end_blocks_rename(self, tail: _Proj) -> None:
        TestReverseReferences._other(tail, "0004_extra")
        assert tail.prepare("--rename", "0002_feature", roots=(_OTHER,)).returncode != 0
        assert not list(tail.out.iterdir())

    def test_reference_only_in_the_target_ledger_blocks_rename_apply(self, tail: _Proj) -> None:
        db = tail.db("target")
        TestReverseReferences._other(tail, "0004_extra")
        tail.bring_up(db)
        tail.ok(db, "upgrade", _OTHER)
        assert tail.prepare("--rename", "0002_feature").returncode == 0
        tail.publish("0002_feature")

        ledger, data = tail.ledger(db), tail.snapshot(db)
        for write in (False, True):
            assert tail.apply(db, "0002_feature", write=write).returncode != 0
        assert (tail.ledger(db), tail.snapshot(db)) == (ledger, data)
