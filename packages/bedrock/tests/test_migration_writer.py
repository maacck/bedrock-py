"""Tests for ``write_revision``: numbering, dependencies, safe writing and dialect rendering."""

import importlib.util
import io
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from bedrock.database.base import BedrockModel
from bedrock.database.migration.graph import MigrationError, MigrationGraph
from bedrock.database.migration.writer import write_revision
from bedrock.module.entities import AppConfig
from bedrock.module.registry import ModuleRegistry
from bedrock.testing.modules import ModuleFactory
from sqlalchemy.dialects import postgresql
from sqlalchemy.types import UserDefinedType


@pytest.fixture(autouse=True)
def isolated_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tables of other suites out of the autogenerate comparison."""
    monkeypatch.setattr(BedrockModel, "metadata", sa.MetaData())


def _script(rev: str, down: str | None, depends_on: tuple[str, ...] = ()) -> str:
    return (
        "from alembic import op\nimport sqlalchemy as sa\n"
        f"revision = {rev!r}\ndown_revision = {down!r}\ndepends_on = {depends_on!r}\n"
        f"def upgrade():\n    op.create_table({rev!r}, sa.Column('id', sa.Integer, primary_key=True))\n"
        f"def downgrade():\n    op.drop_table({rev!r})\n"
    )


Setup = Callable[..., tuple[ModuleRegistry, MigrationGraph]]


@pytest.fixture
def setup(module_factory: ModuleFactory) -> Setup:
    """Create ``(name, scripts, manifest depends_on)`` modules; return their registry and loaded graph."""
    registry = ModuleRegistry()

    def _setup(*apps: tuple[str, dict[str, str], tuple[str, ...]]) -> tuple[ModuleRegistry, MigrationGraph]:
        names = [
            module_factory(
                name,
                manifest={"title": name, "version": "0.1.0", "depends_on": list(deps)},
                models="",
                files={f"migrations/{stem}.py": src for stem, src in scripts.items()},
            )
            for name, scripts, deps in apps
        ]
        registry.populate(names)
        return registry, MigrationGraph.load(registry)

    return _setup


def _load(path: Path) -> ModuleType:
    """Execute a script in a namespace that shares nothing with the test module."""
    spec = importlib.util.spec_from_file_location(f"generated_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _app(registry: ModuleRegistry, name: str) -> AppConfig:
    return registry.get(name)


class TestNumberingAndNaming:
    def test_first_revision_is_manual_and_chains_from_base(self, setup: Setup) -> None:
        registry, graph = setup(("wr_first", {}, ()))
        path = write_revision(_app(registry, "wr_first"), graph, "Initial Schema!", None, None)

        assert path.name == "0001_initial_schema.py"
        module = _load(path)
        assert (module.revision, module.down_revision, module.depends_on) == ("0001_initial_schema", None, ())
        assert "pass" in path.read_text(encoding="utf-8")
        assert MigrationGraph.load(registry).heads("wr_first") == (("wr_first", "0001_initial_schema"),)

    def test_next_number_follows_the_highest_and_legacy_ids_are_kept(self, setup: Setup) -> None:
        registry, graph = setup(
            ("wr_num", {"0007_seven": _script("0007_seven", None)}, ()),
            ("wr_legacy", {"mak111": _script("mak111", None)}, ()),
        )
        assert write_revision(_app(registry, "wr_num"), graph, "next", None, None).name == "0008_next.py"

        path = write_revision(_app(registry, "wr_legacy"), graph, "next", None, None)
        assert path.name == "0001_next.py"
        assert _load(path).down_revision == "mak111"

    def test_empty_message_and_long_message_stay_valid(self, setup: Setup) -> None:
        registry, graph = setup(("wr_slug", {}, ()), ("wr_long", {}, ()))
        path = write_revision(_app(registry, "wr_slug"), graph, "!!!", None, None)
        assert path.name == "0001_migration.py"

        path = write_revision(_app(registry, "wr_long"), graph, "very long message " * 20, None, None)
        assert len(path.stem) <= 64
        assert not path.stem.endswith("_")

    def test_message_cannot_break_the_docstring(self, setup: Setup) -> None:
        registry, graph = setup(("wr_doc", {}, ()))
        message = 'quote """ back\\slash "'
        path = write_revision(_app(registry, "wr_doc"), graph, message, None, None)

        assert MigrationGraph.load(registry)[("wr_doc", path.stem)].message == message

    def test_existing_file_is_never_overwritten(self, setup: Setup) -> None:
        registry, graph = setup(("wr_exists", {}, ()))
        target = _app(registry, "wr_exists").package_dir / "migrations" / "0001_x.py"
        target.parent.mkdir(exist_ok=True)
        target.write_text("keep", encoding="utf-8")

        with pytest.raises(MigrationError, match="already exists"):
            write_revision(_app(registry, "wr_exists"), graph, "x", None, None)
        assert target.read_text(encoding="utf-8") == "keep"

    def test_several_heads_are_rejected_without_writing(self, setup: Setup) -> None:
        registry, graph = setup(
            ("wr_heads", {"0001_a": _script("0001_a", None), "0002_b": _script("0002_b", None)}, ())
        )
        with pytest.raises(MigrationError, match="several migration heads"):
            write_revision(_app(registry, "wr_heads"), graph, "x", None, None)
        assert len(list((_app(registry, "wr_heads").package_dir / "migrations").glob("*.py"))) == 2

    def test_retired_squash_ids_stay_reserved_for_numbering(self, setup: Setup) -> None:
        squash = {"format": 1, "old": [("0002_old", ["wr_ret:0001_base"]), ("0003_old", ["wr_ret:0002_old"])]}
        scripts = {
            "0001_base": _script("0001_base", None),
            "0002_new": _script("0002_new", "0001_base") + f"bedrock_squash = {squash!r}\n",
        }
        registry, graph = setup(("wr_ret", scripts, ()))

        path = write_revision(_app(registry, "wr_ret"), graph, "next", None, None)

        assert path.name == "0004_next.py"

    def test_oversize_successor_is_rejected_without_writing(self, setup: Setup) -> None:
        big = "9" * 62 + "_x"
        registry, graph = setup(("wr_big", {big: _script(big, None)}, ()))

        with pytest.raises(MigrationError, match="longer than 64"):
            write_revision(_app(registry, "wr_big"), graph, "x", None, None)
        assert [p.name for p in (_app(registry, "wr_big").package_dir / "migrations").glob("*.py")] == [f"{big}.py"]


class TestDependencies:
    def test_dependency_heads_are_written_once_and_again_when_upstream_moves(self, setup: Setup) -> None:
        registry, graph = setup(
            ("wr_auth", {"0001_initial": _script("0001_initial", None)}, ()),
            ("wr_orders", {}, ("wr_auth",)),
        )
        orders = _app(registry, "wr_orders")

        first = write_revision(orders, graph, "initial", None, None)
        assert _load(first).depends_on == ("wr_auth:0001_initial",)

        graph = MigrationGraph.load(registry)
        second = write_revision(orders, graph, "more", None, None)
        assert _load(second).depends_on == ()
        assert _load(second).down_revision == first.stem

        auth_next = write_revision(_app(registry, "wr_auth"), graph, "email", None, None)
        graph = MigrationGraph.load(registry)
        third = write_revision(orders, graph, "again", None, None)
        assert _load(third).depends_on == (f"wr_auth:{auth_next.stem}",)

    def test_indirect_dependencies_are_not_written(self, setup: Setup) -> None:
        registry, graph = setup(
            ("wr_base", {"0001_initial": _script("0001_initial", None)}, ()),
            ("wr_mid", {"0001_initial": _script("0001_initial", None, ("wr_base:0001_initial",))}, ("wr_base",)),
            ("wr_top", {}, ("wr_mid",)),
        )
        assert _load(write_revision(_app(registry, "wr_top"), graph, "initial", None, None)).depends_on == (
            "wr_mid:0001_initial",
        )


class TestAutogenerate:
    @staticmethod
    def _owned_table(app: str) -> dict[str, str]:
        sa.Table(
            "wr_jsonb",
            BedrockModel.metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("data", postgresql.JSONB),
            sa.Column("tags", postgresql.ARRAY(postgresql.JSONB)),
            sa.Column("kind", postgresql.ENUM("a", "b", name="wr_kind")),
        )
        sa.Table("wr_foreign", BedrockModel.metadata, sa.Column("id", sa.Integer, primary_key=True))
        return {"wr_jsonb": app, "wr_foreign": "someone_else"}

    def test_owners_are_required_with_a_connection(self, setup: Setup) -> None:
        registry, graph = setup(("wr_noown", {}, ()))
        engine = sa.create_engine("sqlite://")
        with engine.connect() as conn, pytest.raises(MigrationError, match="ownership"):
            write_revision(_app(registry, "wr_noown"), graph, "x", conn, None)
        assert not (_app(registry, "wr_noown").package_dir / "migrations").exists()

    def test_generated_dialect_types_execute_in_a_cold_namespace(self, setup: Setup) -> None:
        registry, graph = setup(("wr_dialect", {}, ()))
        owners = self._owned_table("wr_dialect")
        with sa.create_engine("sqlite://").connect() as conn:
            path = write_revision(_app(registry, "wr_dialect"), graph, "jsonb", conn, owners)

        source = path.read_text(encoding="utf-8")
        assert "from sqlalchemy.dialects import postgresql" in source
        assert "wr_foreign" not in source

        module = _load(path)
        for step in (module.upgrade, module.downgrade):
            buffer = io.StringIO()
            context = MigrationContext.configure(
                dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer}
            )
            with Operations.context(context):
                step()
        assert "wr_jsonb" in buffer.getvalue()

    def test_unrenderable_type_writes_nothing(self, setup: Setup) -> None:
        class Opaque(UserDefinedType[object]):
            cache_ok = True

            def get_col_spec(self, **_kw: object) -> str:
                return "OPAQUE"

        registry, graph = setup(("wr_opaque", {}, ()))
        sa.Table(
            "wr_opaque_t",
            BedrockModel.metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("x", Opaque()),
        )
        with sa.create_engine("sqlite://").connect() as conn, pytest.raises(MigrationError, match="by hand"):
            write_revision(_app(registry, "wr_opaque"), graph, "x", conn, {"wr_opaque_t": "wr_opaque"})
        assert not (_app(registry, "wr_opaque").package_dir / "migrations").exists()

    def test_no_changes_render_valid_pass_bodies(self, setup: Setup) -> None:
        registry, graph = setup(("wr_clean", {}, ()))
        with sa.create_engine("sqlite://").connect() as conn:
            path = write_revision(_app(registry, "wr_clean"), graph, "nothing", conn, {})

        module = _load(path)
        module.upgrade()
        module.downgrade()
