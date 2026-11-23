"""Tests for ``prepare_candidate``: syntax allow-list, replay, hand-written scripts and output safety."""

import ast
import hashlib
import importlib.util
import io
import re
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from bedrock.database.base import BedrockModel
from bedrock.database.migration.graph import MigrationError, MigrationGraph
from bedrock.database.migration.squash import build_spec, installed_specs
from bedrock.database.migration.squash_writer import prepare_candidate
from bedrock.database.migration.writer import _SCRIPT_TEMPLATE, write_revision
from bedrock.module.registry import ModuleRegistry
from bedrock.testing.modules import ModuleFactory


@pytest.fixture(autouse=True)
def isolated_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(BedrockModel, "metadata", sa.MetaData())


def _src(rev: str, down: str | None, depends: tuple[str, ...] = (), ups: str = "pass", downs: str = "pass") -> str:
    """A script in the exact shape ``write_revision`` renders."""
    return _SCRIPT_TEMPLATE.format(
        message=rev,
        revision=rev,
        revises=down or "",
        create_date="2026-01-01",
        imports="",
        down_revision=down,
        depends_on=depends,
        upgrades=ups.replace("\n", "\n    "),
        downgrades=downs.replace("\n", "\n    "),
    )


_ADD_NAME = "op.add_column('sq_items', sa.Column('name', sa.String(length=50), nullable=True))"
_LOG_TABLE = """op.create_table('sq_log',
sa.Column('id', sa.Integer(), nullable=False),
sa.Column('item_id', sa.Integer(), nullable=True),
sa.Column('flag', sa.Boolean(), server_default='0', nullable=False),
sa.ForeignKeyConstraint(['item_id'], ['sq_items.id'], name='fk_log_item', ondelete='CASCADE'),
sa.UniqueConstraint('item_id', name='uq_log_item'),
sa.PrimaryKeyConstraint('id'))
op.create_index(op.f('ix_sq_log_item_id'), 'sq_log', ['item_id'], unique=False)"""
_DROP_LOG = "op.drop_index(op.f('ix_sq_log_item_id'), table_name='sq_log')\nop.drop_table('sq_log')"
_ADD_ACTIVE = "op.add_column('sq_items', sa.Column('active', sa.Boolean(), server_default='1', nullable=False))"


def _scripts(auth: str) -> dict[str, str]:
    return {
        "0001_base": _src(
            "0001_base",
            None,
            ups="op.create_table('sq_items', sa.Column('id', sa.Integer(), nullable=False), "
            "sa.PrimaryKeyConstraint('id'))",
            downs="op.drop_table('sq_items')",
        ),
        "0002_add": _src(
            "0002_add",
            "0001_base",
            (f"{auth}:0001_init",),
            _ADD_NAME,
            "op.drop_column('sq_items', 'name')",
        ),
        "0003_fill": _src("0003_fill", "0002_add", ups=_LOG_TABLE, downs=_DROP_LOG),
        "0004_final": _src("0004_final", "0003_fill", ups=_ADD_ACTIVE, downs="op.drop_column('sq_items', 'active')"),
        "0005_ship": _src("0005_ship", "0004_final"),
    }


Setup = Callable[..., tuple[ModuleRegistry, MigrationGraph, str]]


@pytest.fixture
def setup(module_factory: ModuleFactory) -> Setup:
    """Create ``<name>_auth`` and ``<name>`` (orders-like, five scripts); return registry, graph and the app name."""

    def _setup(name: str, overrides: dict[str, str] | None = None) -> tuple[ModuleRegistry, MigrationGraph, str]:
        auth = f"{name}_auth"
        scripts = {**_scripts(auth), **(overrides or {})}
        names = [
            module_factory(
                auth,
                manifest={"title": auth, "version": "0.1.0", "depends_on": []},
                models="",
                files={"migrations/0001_init.py": _src("0001_init", None)},
            ),
            module_factory(
                name,
                manifest={"title": name, "version": "0.1.0", "depends_on": [auth]},
                models="",
                files={f"migrations/{stem}.py": text for stem, text in scripts.items()},
            ),
        ]
        registry = ModuleRegistry()
        registry.populate(names)
        return registry, MigrationGraph.load(registry), name

    return _setup


def _load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"squash_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sql(*steps: Callable[[], None]) -> str:
    buffer = io.StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer})
    with Operations.context(context):
        for step in steps:
            step()
    return buffer.getvalue()


def _old(graph: MigrationGraph, app: str, names: list[str]) -> list[ModuleType]:
    return [_load(graph[(app, name)].path) for name in names]


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _line(source: str, marker: str) -> int:
    return next(i for i, line in enumerate(source.splitlines(), 1) if marker in line)


class TestAutomaticRendering:
    def test_replay_matches_the_old_chain_in_order_and_reversed_on_downgrade(
        self, setup: Setup, tmp_path: Path
    ) -> None:
        registry, graph, app = setup("sq_replay")
        names = ["0002_add", "0003_fill", "0004_final"]
        old = _old(graph, app, names)

        path = prepare_candidate(registry, app, "0002_add", "0004_final", out=tmp_path / "candidate")

        candidate = _load(path)
        assert path == (tmp_path / "candidate" / "0004_final.py").resolve()
        assert _sql(candidate.upgrade) == _sql(*(m.upgrade for m in old))
        assert _sql(candidate.downgrade) == _sql(*(m.downgrade for m in reversed(old)))
        assert (candidate.revision, candidate.down_revision) == ("0004_final", "0001_base")
        assert candidate.depends_on == ("sq_replay_auth:0001_init",)
        assert candidate.bedrock_squash == build_spec(graph, app, "0002_add", "0004_final").metadata()

    def test_duplicate_looking_steps_are_kept(self, setup: Setup, tmp_path: Path) -> None:
        step = "op.add_column('sq_items', sa.Column('n', sa.Integer(), nullable=True))"
        drop = "op.drop_column('sq_items', 'n')"
        registry, graph, app = setup(
            "sq_dup",
            {
                "0002_add": _src("0002_add", "0001_base", ("sq_dup_auth:0001_init",), step, drop),
                "0003_fill": _src("0003_fill", "0002_add", ups=step, downs=drop),
            },
        )
        candidate = _load(prepare_candidate(registry, app, "0002_add", "0003_fill", out=tmp_path / "c"))

        assert _sql(candidate.upgrade).count("ADD COLUMN n") == 2
        assert _sql(candidate.downgrade).count("DROP COLUMN n") == 2

    def test_empty_bodies_stay_valid(self, setup: Setup, tmp_path: Path) -> None:
        registry, graph, app = setup(
            "sq_empty",
            {
                "0002_add": _src("0002_add", "0001_base", ("sq_empty_auth:0001_init",)),
                "0003_fill": _src("0003_fill", "0002_add"),
            },
        )
        candidate = _load(prepare_candidate(registry, app, "0002_add", "0003_fill", out=tmp_path / "c"))

        candidate.upgrade()
        candidate.downgrade()

    def test_writer_autogenerated_scripts_replay_on_a_real_database(
        self, module_factory: ModuleFactory, tmp_path: Path
    ) -> None:
        name = module_factory(
            "sq_auto", manifest={"title": "a", "version": "0.1.0", "depends_on": []}, models="", files={}
        )
        registry = ModuleRegistry()
        registry.populate([name])
        app = registry.get(name)
        engine = sa.create_engine("sqlite://")
        written: list[Path] = []
        with engine.connect() as conn:
            for extra in ([], [sa.Column("age", sa.Integer())]):
                metadata = sa.MetaData()
                sa.Table(
                    "sq_t",
                    metadata,
                    sa.Column("id", sa.Integer, primary_key=True),
                    sa.Column("name", sa.String(50), index=True),
                    *extra,
                )
                BedrockModel.metadata = metadata  # type: ignore[misc]
                path = write_revision(app, MigrationGraph.load(registry), "step", conn, {"sq_t": name})
                written.append(path)
                with Operations.context(MigrationContext.configure(conn)):
                    _load(path).upgrade()

        def schema(steps: list[Callable[[], None]]) -> dict[str, object]:
            with sa.create_engine("sqlite://").connect() as fresh:
                with Operations.context(MigrationContext.configure(fresh)):
                    for step in steps:
                        step()
                inspector = sa.inspect(fresh)
                return {
                    table: (
                        [(c["name"], str(c["type"]), c["nullable"]) for c in inspector.get_columns(table)],
                        sorted(i["name"] for i in inspector.get_indexes(table)),
                    )
                    for table in inspector.get_table_names()
                }

        first, last = written[0].stem, written[1].stem
        candidate = _load(prepare_candidate(registry, name, first, last, out=tmp_path / "c"))

        assert schema([candidate.upgrade]) == schema([m.upgrade for m in map(_load, written)]) != {}
        assert schema([candidate.upgrade, candidate.downgrade]) == {}


class TestRenameAndManifest:
    def test_rename_names_the_file_and_revision_and_lists_every_old_file(self, setup: Setup, tmp_path: Path) -> None:
        registry, graph, app = setup("sq_rename")

        path = prepare_candidate(registry, app, "0002_add", "0005_ship", out=tmp_path / "c", rename="0002_feature")

        assert path.name == "0002_feature.py"
        assert _load(path).revision == "0002_feature"
        listing = (tmp_path / "c" / "0002_feature.files.txt").read_text(encoding="utf-8")
        for stem in ("0002_add", "0003_fill", "0004_final", "0005_ship"):
            assert str(graph[(app, stem)].path) in listing
        assert "Replace" not in listing

    def test_default_keeps_the_end_id_and_replaces_its_file(self, setup: Setup, tmp_path: Path) -> None:
        registry, graph, app = setup("sq_keep")
        path = prepare_candidate(registry, app, "0002_add", "0004_final", out=tmp_path / "c")

        listing = path.with_suffix(".files.txt").read_text(encoding="utf-8")
        assert f"Replace {graph[(app, '0004_final')].path} with the candidate" in listing
        assert str(graph[(app, "0002_add")].path) in listing
        assert str(graph[(app, "0003_fill")].path) in listing
        assert "ordinary `bedrock db upgrade`" in listing and "bridge release" in listing
        assert "read-only; `--apply` switches manually" in listing
        assert "0005_ship" not in listing

    def test_candidate_replaces_the_range_in_a_real_tree(self, setup: Setup, tmp_path: Path) -> None:
        registry, graph, app = setup("sq_tree")
        path = prepare_candidate(registry, app, "0002_add", "0004_final", out=tmp_path / "c")
        directory = graph[(app, "0004_final")].path.parent
        graph[(app, "0002_add")].path.unlink()
        graph[(app, "0003_fill")].path.unlink()
        (directory / "0004_final.py").write_bytes(path.read_bytes())

        fresh = MigrationGraph.load(registry)

        assert {r for _, r in fresh.revisions(app)} == {"0001_base", "0004_final", "0005_ship"}
        assert fresh[(app, "0004_final")].down_revisions == ((app, "0001_base"),)
        assert fresh[(app, "0005_ship")].down_revisions == ((app, "0004_final"),)
        assert [spec.old[0][0] for spec in installed_specs(fresh).values()] == [(app, "0002_add")]

    def test_rename_onto_an_unrelated_active_file_is_rejected(self, setup: Setup, tmp_path: Path) -> None:
        registry, graph, app = setup("sq_clash", {"0002_feature": _src("0009_other", "0001_base")})

        with pytest.raises(MigrationError, match="already exists in the active migrations"):
            prepare_candidate(registry, app, "0002_add", "0005_ship", out=tmp_path / "c", rename="0002_feature")

        assert not (tmp_path / "c").exists()

    def test_rename_onto_an_old_files_name_replaces_it_instead_of_add_and_delete(
        self, setup: Setup, tmp_path: Path
    ) -> None:
        registry, graph, app = setup("sq_same")
        old = graph[(app, "0005_ship")].path
        target = old.with_name("0009_x.py")
        old.rename(target)

        path = prepare_candidate(registry, app, "0002_add", "0005_ship", out=tmp_path / "c", rename="0009_x")

        listing = path.with_suffix(".files.txt").read_text(encoding="utf-8")
        assert f"Replace {target} with the candidate" in listing
        assert f"       {target}" not in listing

    def test_cookie_encoded_old_script_is_read_in_its_own_encoding(self, setup: Setup, tmp_path: Path) -> None:
        registry, graph, app = setup("sq_auto_cookie")
        note = "op.add_column('sq_items', sa.Column('note', sa.String(length=5), comment='café', nullable=True))"
        body = _src("0003_fill", "0002_add", ups=note, downs="op.drop_column('sq_items', 'note')")
        graph[(app, "0003_fill")].path.write_bytes(("# -*- coding: latin-1 -*-\n" + body).encode("latin-1"))

        path = prepare_candidate(registry, app, "0002_add", "0004_final", out=tmp_path / "c")

        assert "comment='café'" in path.read_text(encoding="utf-8")


class TestUnsupportedSyntax:
    @pytest.mark.parametrize(
        ("ups", "marker"),
        [
            ("op.execute(\"update sq_items set name = 'x'\")", "op.execute"),
            ("_helper()", "_helper"),
            ("op.create_table(table_name, sa.Column('id', sa.Integer(), nullable=False))", "table_name"),
            ("op.add_column('sq_items', sa.Column('x', sa.Integer(), server_default=sa.text('1')))", "sa.text"),
            ("op.add_column('sq_items', sa.Column('x', sa.Enum('a', 'b', name='e')))", "sa.Enum"),
            ("op.create_table('t', *columns)", "*columns"),
            ("if True:\n    op.drop_table('x')", "if True"),
            ("op.create_index('i', 't', ['a'], postgresql_where='a > 1')", "postgresql_where"),
            ("x = 1", "x = 1"),
        ],
    )
    def test_body_is_rejected_with_file_and_line(self, setup: Setup, tmp_path: Path, ups: str, marker: str) -> None:
        text = _src("0003_fill", "0002_add", ups=ups)
        registry, graph, app = setup("sq_body", {"0003_fill": text})
        path = graph[(app, "0003_fill")].path
        before = _digest(path)

        with pytest.raises(MigrationError, match=rf"{re.escape(str(path))}:{_line(text, marker)}: .*--script"):
            prepare_candidate(registry, app, "0002_add", "0004_final", out=tmp_path / "c")

        assert not (tmp_path / "c").exists()
        assert _digest(path) == before

    @pytest.mark.parametrize(
        ("old", "new", "marker"),
        [
            ("def upgrade() -> None:", "async def upgrade() -> None:", "async def upgrade"),
            ("def upgrade() -> None:", "@staticmethod\ndef upgrade() -> None:", "def upgrade"),
            ("def upgrade() -> None:", "def upgrade[T]() -> None:", "def upgrade"),
            ("def upgrade() -> None:", "def upgrade(x) -> None:", "def upgrade"),
            ("import sqlalchemy as sa", "import sqlalchemy as sa\nimport os", "import os"),
            ("from alembic import op", "from alembic import op as o", "as o"),
            ("\n\ndef upgrade", "\n\ndef _helper():\n    pass\n\n\ndef upgrade", "def _helper"),
        ],
    )
    def test_top_level_is_rejected_with_file_and_line(
        self, setup: Setup, tmp_path: Path, old: str, new: str, marker: str
    ) -> None:
        text = _src("0003_fill", "0002_add", ups="pass").replace(old, new, 1)
        registry, graph, app = setup("sq_top", {"0003_fill": text})
        path = graph[(app, "0003_fill")].path

        with pytest.raises(MigrationError, match=rf"{re.escape(str(path))}:{_line(text, marker)}: .*--script"):
            prepare_candidate(registry, app, "0002_add", "0004_final", out=tmp_path / "c")

        assert not (tmp_path / "c").exists()


_MANUAL = """from alembic import op


revision = {revision!r}
down_revision = {down!r}
depends_on = {depends!r}


def upgrade():
    op.execute("update sq_items set name = 'x'")


def downgrade():
    pass
"""


def _manual(**overrides: object) -> str:
    values: dict[str, object] = {
        "revision": "0004_final",
        "down": "0001_base",
        "depends": ("sq_manual_auth:0001_init",),
    }
    return _MANUAL.format(**{**values, **overrides})


class TestHandWrittenScript:
    def test_valid_script_is_kept_verbatim_and_gets_the_squash_literal(self, setup: Setup, tmp_path: Path) -> None:
        registry, graph, app = setup("sq_manual")
        script = tmp_path / "mine.py"
        script.write_text(_manual(), encoding="utf-8")

        path = prepare_candidate(registry, app, "0002_add", "0004_final", out=tmp_path / "c", script=script)

        text = path.read_text(encoding="utf-8")
        assert text.startswith(_manual().rstrip("\n"))
        assert "update sq_items set name" in text
        assert _load(path).bedrock_squash == build_spec(graph, app, "0002_add", "0004_final").metadata()
        assert script.read_text(encoding="utf-8") == _manual()

    @pytest.mark.parametrize(
        "source",
        [
            _manual(revision="0009_other"),
            _manual(down="0002_add"),
            _manual(depends=()),
            _manual() + "\nbedrock_squash = {}\n",
            _manual().replace("def upgrade", "async def upgrade"),
            _manual().replace("op.execute", "yield op.execute"),
            _manual().replace("def upgrade():\n", "def upgrade():\n    def f(x=(yield)):\n        pass\n"),
            _manual().replace("def downgrade():\n", "def downgrade():\n    def f(x=(yield from ())):\n        pass\n"),
            _manual(depends=False),
            _manual(depends=0),
            _manual(down=False),
            _manual(down=0),
            _manual(down=None, depends=("sq_manual:0001_base", "sq_manual_auth:0001_init")),
            _manual(depends=("sq_manual_auth:0001_init", "")),
            _manual().replace("def upgrade()", "def upgrade(x)"),
            _manual().replace("def upgrade()", "def upgrade[T]()"),
            _manual().replace("def downgrade()", "def downgrade[T]()"),
            _manual().replace("def upgrade", "@cache\ndef upgrade"),
            _manual() + "\nupgrade = lambda: None\n",
            _manual() + "\ndef downgrade():\n    pass\n",
            _manual().replace("def downgrade():\n    pass\n", ""),
            _manual() + "\nfrom os import *\n",
            _manual().replace("revision = '0004_final'", "revision = make()"),
            _manual() + "\nrevision += 'x'\n",
        ],
    )
    def test_invalid_script_is_rejected_without_output(self, setup: Setup, tmp_path: Path, source: str) -> None:
        registry, graph, app = setup("sq_manual")
        script = tmp_path / "mine.py"
        script.write_text(source, encoding="utf-8")

        with pytest.raises(MigrationError, match=rf"{re.escape(str(script))}:\d+: "):
            prepare_candidate(registry, app, "0002_add", "0004_final", out=tmp_path / "c", script=script)

        assert not (tmp_path / "c").exists()
        assert script.read_text(encoding="utf-8") == source

    def test_loader_shaped_parents_are_accepted(self, setup: Setup, tmp_path: Path) -> None:
        registry, graph, app = setup("sq_shape")
        script = tmp_path / "mine.py"
        script.write_text(_manual(down=["0001_base"], depends="sq_shape_auth:0001_init"), encoding="utf-8")

        path = prepare_candidate(registry, app, "0002_add", "0004_final", out=tmp_path / "c", script=script)

        assert _load(path).revision == "0004_final"

    def test_cookie_encoding_is_read_and_written_back_consistently(self, setup: Setup, tmp_path: Path) -> None:
        registry, graph, app = setup("sq_cookie")
        script = tmp_path / "mine.py"
        source = "# -*- coding: latin-1 -*-\n" + _manual(depends="sq_cookie_auth:0001_init").replace("'x'", "'café'")
        script.write_bytes(source.encode("latin-1"))

        path = prepare_candidate(registry, app, "0002_add", "0004_final", out=tmp_path / "c", script=script)

        assert "café" in ast.dump(ast.parse(path.read_bytes()))  # what the graph loader compiles
        assert path.read_bytes().decode("latin-1").startswith("# -*- coding: latin-1 -*-")

    def test_undecodable_script_is_rejected_without_output(self, setup: Setup, tmp_path: Path) -> None:
        registry, graph, app = setup("sq_bytes")
        script = tmp_path / "mine.py"
        script.write_bytes(_manual().encode() + b"# \xff\n")

        with pytest.raises(MigrationError, match="Cannot read squash script"):
            prepare_candidate(registry, app, "0002_add", "0004_final", out=tmp_path / "c", script=script)

        assert not (tmp_path / "c").exists()


class TestOutputSafety:
    def test_out_inside_active_migrations_is_rejected(self, setup: Setup) -> None:
        registry, graph, app = setup("sq_active")
        directory = graph[(app, "0004_final")].path.parent
        before = sorted(p.name for p in directory.iterdir())

        for out in (directory, directory / "sub"):
            with pytest.raises(MigrationError, match="active migrations"):
                prepare_candidate(registry, app, "0002_add", "0004_final", out=out)

        assert sorted(p.name for p in directory.iterdir()) == before

    def test_symlinked_out_to_active_migrations_is_rejected(self, setup: Setup, tmp_path: Path) -> None:
        registry, graph, app = setup("sq_link")
        directory = graph[(app, "0004_final")].path.parent
        link = tmp_path / "link"
        link.symlink_to(directory, target_is_directory=True)
        before = _digest(graph[(app, "0004_final")].path)

        with pytest.raises(MigrationError, match="symbolic link"):
            prepare_candidate(registry, app, "0002_add", "0004_final", out=link)

        assert _digest(graph[(app, "0004_final")].path) == before

    def test_existing_candidate_is_never_overwritten(self, setup: Setup, tmp_path: Path) -> None:
        registry, graph, app = setup("sq_exists")
        out = tmp_path / "c"
        out.mkdir()
        (out / "0004_final.py").write_text("keep", encoding="utf-8")

        with pytest.raises(MigrationError, match="already exists"):
            prepare_candidate(registry, app, "0002_add", "0004_final", out=out)

        assert (out / "0004_final.py").read_text(encoding="utf-8") == "keep"
        assert [p.name for p in out.iterdir()] == ["0004_final.py"]

    def test_second_prepare_into_the_same_out_is_rejected(self, setup: Setup, tmp_path: Path) -> None:
        registry, graph, app = setup("sq_twice")
        prepare_candidate(registry, app, "0002_add", "0004_final", out=tmp_path / "c")

        with pytest.raises(MigrationError, match="already exists"):
            prepare_candidate(registry, app, "0002_add", "0004_final", out=tmp_path / "c")

    def test_out_that_is_a_file_is_rejected(self, setup: Setup, tmp_path: Path) -> None:
        registry, graph, app = setup("sq_file")
        (tmp_path / "f").write_text("x", encoding="utf-8")

        with pytest.raises(MigrationError, match="not a directory"):
            prepare_candidate(registry, app, "0002_add", "0004_final", out=tmp_path / "f")

    def test_script_that_is_the_candidate_path_is_never_clobbered(self, setup: Setup, tmp_path: Path) -> None:
        registry, graph, app = setup("sq_manual")
        out = tmp_path / "c"
        out.mkdir()
        script = out / "0004_final.py"
        script.write_text(_manual(), encoding="utf-8")

        with pytest.raises(MigrationError, match="already exists"):
            prepare_candidate(registry, app, "0002_add", "0004_final", out=out, script=script)

        assert script.read_text(encoding="utf-8") == _manual()

    def test_failed_write_leaves_no_file_and_no_new_directory(
        self, setup: Setup, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry, graph, app = setup("sq_fail")
        sources = {k: _digest(graph[k].path) for k in graph.migrations}
        real_open = Path.open

        def flaky(self: Path, mode: str = "r", *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            if self.name.endswith(".files.txt") and "x" in mode:
                raise OSError("disk full")
            return real_open(self, mode, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "open", flaky)
        with pytest.raises(MigrationError, match="Cannot write"):
            prepare_candidate(registry, app, "0002_add", "0004_final", out=tmp_path / "new" / "c")

        assert not (tmp_path / "new").exists()
        assert {k: _digest(graph[k].path) for k in graph.migrations} == sources

    def test_invalid_range_writes_nothing(self, setup: Setup, tmp_path: Path) -> None:
        registry, graph, app = setup("sq_range")

        with pytest.raises(MigrationError):
            prepare_candidate(registry, app, "0004_final", "0002_add", out=tmp_path / "c")

        assert not (tmp_path / "c").exists()
