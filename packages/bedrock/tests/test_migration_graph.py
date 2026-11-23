from collections.abc import Callable

import pytest
from bedrock.database.migration.graph import Migration, MigrationError, MigrationGraph
from bedrock.exc import BedrockExc
from bedrock.module.registry import ModuleRegistry
from bedrock.testing.modules import ModuleFactory


def _script(rev: str, down: str | None, depends_on: tuple[str, ...] = ()) -> str:
    """A revision that creates (and drops) a table named after itself."""
    return (
        "from alembic import op\nimport sqlalchemy as sa\n"
        f"revision = {rev!r}\ndown_revision = {down!r}\ndepends_on = {depends_on!r}\n"
        f"def upgrade():\n    op.create_table({rev!r}, sa.Column('id', sa.Integer, primary_key=True))\n"
        f"def downgrade():\n    op.drop_table({rev!r})\n"
    )


def _chain_app(factory: ModuleFactory, name: str, scripts: dict[str, str], depends_on: tuple[str, ...] = ()) -> str:
    return factory(
        name,
        manifest={"title": name, "version": "0.1.0", "depends_on": list(depends_on)},
        models="",
        files={f"migrations/{stem}.py": src for stem, src in scripts.items()},
    )


@pytest.fixture
def load(module_factory: ModuleFactory) -> Callable[..., MigrationGraph]:
    """Create modules from ``(name, scripts, manifest depends_on)`` triples and load their graph."""

    def _load(*apps: tuple[str, dict[str, str], tuple[str, ...]]) -> MigrationGraph:
        names = [_chain_app(module_factory, name, scripts, deps) for name, scripts, deps in apps]
        registry = ModuleRegistry()
        registry.populate(names)
        return MigrationGraph.load(registry)

    return _load


def _two_apps(load: Callable[..., MigrationGraph]) -> MigrationGraph:
    return load(
        (
            "gr_auth",
            {"0001_initial": _script("0001_initial", None), "0002_email": _script("0002_email", "0001_initial")},
            (),
        ),
        ("gr_orders", {"0001_initial": _script("0001_initial", None, ("gr_auth:0002_email",))}, ("gr_auth",)),
    )


class TestMigrationError:
    def test_is_bedrock_and_runtime_error(self) -> None:
        err = MigrationError("boom", "app")
        assert isinstance(err, BedrockExc)
        assert isinstance(err, RuntimeError)
        assert err.app_import_path == "app"
        assert err.detail == "boom"


class TestMigrationGraph:
    def test_same_name_in_two_apps_are_distinct_nodes(self, load: Callable[..., MigrationGraph]) -> None:
        graph = _two_apps(load)

        assert set(graph.migrations) == {
            ("gr_auth", "0001_initial"),
            ("gr_auth", "0002_email"),
            ("gr_orders", "0001_initial"),
        }
        assert isinstance(graph[("gr_orders", "0001_initial")], Migration)
        assert graph[("gr_orders", "0001_initial")].depends_on == (("gr_auth", "0002_email"),)
        assert ("gr_auth", "0001_initial") in graph
        assert ("gr_other", "0001_initial") not in graph
        assert graph.revisions("gr_auth") == {("gr_auth", "0001_initial"), ("gr_auth", "0002_email")}
        assert graph.heads("gr_auth") == (("gr_auth", "0002_email"),)
        assert graph.heads("gr_orders") == (("gr_orders", "0001_initial"),)

    def test_order_and_closures(self, load: Callable[..., MigrationGraph]) -> None:
        graph = _two_apps(load)
        a1, a2, o1 = ("gr_auth", "0001_initial"), ("gr_auth", "0002_email"), ("gr_orders", "0001_initial")

        assert graph.order([o1, a2, a1]) == [a1, a2, o1]
        assert graph.order([o1, a1]) == [a1, o1]  # missing ancestors are not added
        assert graph.ancestors([o1]) == {a1, a2, o1}
        assert graph.ancestors([a1]) == {a1}
        assert graph.descendants_in_app([a1]) == {a1, a2}  # orders:o1 depends on auth but is another app
        assert graph.descendants_in_app([o1]) == {o1}
        assert graph.order([]) == []

    def test_unknown_keys_are_rejected(self, load: Callable[..., MigrationGraph]) -> None:
        graph = _two_apps(load)
        missing = ("gr_orders", "0002_email")  # exists only under gr_auth

        for query in (graph.ancestors, graph.descendants_in_app, graph.order):
            with pytest.raises(MigrationError, match="gr_orders:0002_email"):
                query([missing])

    def test_order_is_deterministic_for_independent_apps(self, load: Callable[..., MigrationGraph]) -> None:
        graph = load(
            ("gr_zed", {"0001_initial": _script("0001_initial", None)}, ()),
            ("gr_alpha", {"0001_initial": _script("0001_initial", None)}, ()),
        )

        assert graph.order(graph.migrations) == [("gr_alpha", "0001_initial"), ("gr_zed", "0001_initial")]

    def test_multiple_same_app_heads_are_sorted(self, load: Callable[..., MigrationGraph]) -> None:
        graph = load(
            (
                "gr_fork",
                {
                    "root": _script("root", None),
                    "right": _script("right", "root"),
                    "left": _script("left", "root"),
                },
                (),
            )
        )

        assert graph.heads("gr_fork") == (("gr_fork", "left"), ("gr_fork", "right"))
        assert graph.heads("gr_missing") == ()

    def test_merge_revision_with_two_parents(self, load: Callable[..., MigrationGraph]) -> None:
        merge = _script("merge", None).replace("down_revision = None", "down_revision = ('left', 'right')")
        graph = load(
            (
                "gr_merge",
                {
                    "root": _script("root", None),
                    "left": _script("left", "root"),
                    "right": _script("right", "root"),
                    "merge": merge,
                },
                (),
            )
        )

        assert graph.heads("gr_merge") == (("gr_merge", "merge"),)
        assert graph.order(graph.migrations)[0] == ("gr_merge", "root")
        assert graph.order(graph.migrations)[-1] == ("gr_merge", "merge")

    def test_underscore_files_skipped_and_branch_labels_ignored(self, load: Callable[..., MigrationGraph]) -> None:
        legacy = _script("a1", None) + "branch_labels = ('gr_legacy',)\n"
        graph = load(("gr_legacy", {"a1": legacy, "__init__": "raise RuntimeError", "_helper": "x ="}, ()))

        assert set(graph.migrations) == {("gr_legacy", "a1")}

    def test_message_is_first_docstring_line(self, load: Callable[..., MigrationGraph]) -> None:
        graph = load(("gr_doc", {"a1": '"""Add things.\n\nRevision ID: a1\n"""\n' + _script("a1", None)}, ()))

        assert graph[("gr_doc", "a1")].message == "Add things."

    @pytest.mark.parametrize(
        ("case", "match"),
        [
            ("duplicate", "Duplicate revision '0001_initial'"),
            ("foreign_down", "not a revision of app 'gr_b'"),
            ("unknown_dep", "unknown migration 'gr_a:nope'"),
            ("bare_dep", "qualified 'app:revision'"),
            ("undeclared_dep", "not a direct manifest dependency"),
            ("transitive_dep", "not a direct manifest dependency"),
            ("cycle", "cycle: .*->"),
            ("self_cycle", "cycle"),
            ("missing_revision", "string 'revision'"),
            ("bad_down_type", "down_revision must be"),
            ("bad_depends_type", "depends_on must be"),
            ("long_revision", "longer than 64"),
            ("broken_script", "Cannot load migration script"),
        ],
    )
    def test_invalid_graph_is_rejected(self, load: Callable[..., MigrationGraph], case: str, match: str) -> None:
        a1 = _script("0001_initial", None)
        apps: dict[str, tuple[str, dict[str, str], tuple[str, ...]]] = {
            "duplicate": ("gr_a", {"x": a1, "y": a1}, ()),
            "foreign_down": ("gr_b", {"b1": _script("b1", "0001_initial")}, ("gr_a",)),
            "unknown_dep": ("gr_b", {"b1": _script("b1", None, ("gr_a:nope",))}, ("gr_a",)),
            "bare_dep": ("gr_b", {"b1": _script("b1", None, ("0001_initial",))}, ("gr_a",)),
            "undeclared_dep": ("gr_b", {"b1": _script("b1", None, ("gr_a:0001_initial",))}, ()),
            "transitive_dep": ("gr_c", {"c1": _script("c1", None, ("gr_a:0001_initial",))}, ("gr_b",)),
            "cycle": ("gr_b", {"x": _script("x", "y"), "y": _script("y", "x")}, ()),
            "self_cycle": ("gr_b", {"x": _script("x", "x")}, ()),
            "missing_revision": ("gr_b", {"x": "value = 1\n"}, ()),
            "bad_down_type": (
                "gr_b",
                {"x": _script("x", None).replace("down_revision = None", "down_revision = 1")},
                (),
            ),
            "bad_depends_type": ("gr_b", {"x": _script("x", None).replace("depends_on = ()", "depends_on = 5")}, ()),
            "long_revision": ("gr_b", {"x": _script("r" * 65, None)}, ()),
            "broken_script": ("gr_b", {"x": "raise ValueError('nope')\n"}, ()),
        }
        graph_apps = [apps[case]]
        if case in {"foreign_down", "unknown_dep", "bare_dep"}:
            graph_apps.insert(0, ("gr_a", {"0001_initial": a1}, ()))
        elif case == "undeclared_dep":
            graph_apps.insert(0, ("gr_a", {"0001_initial": a1}, ()))
        elif case == "transitive_dep":
            graph_apps[:0] = [("gr_a", {"0001_initial": a1}, ()), ("gr_b", {"b1": _script("b1", None)}, ("gr_a",))]

        with pytest.raises(MigrationError, match=match):
            load(*graph_apps)

    def test_direct_graph_rejects_foreign_down_and_unknown_edges(self, load: Callable[..., MigrationGraph]) -> None:
        graph = _two_apps(load)
        o1 = graph[("gr_orders", "0001_initial")]
        a1 = graph[("gr_auth", "0001_initial")]

        def with_(mig: Migration, **changes: object) -> dict[tuple[str, str], Migration]:
            data = {**mig.__dict__, **changes}
            return {**graph.migrations, mig.key: Migration(**data)}  # type: ignore[arg-type]

        with pytest.raises(MigrationError, match="not a revision of app"):
            MigrationGraph(with_(o1, down_revisions=(("gr_auth", "0001_initial"),)))
        with pytest.raises(MigrationError, match="unknown migration"):
            MigrationGraph(with_(a1, depends_on=(("gr_auth", "ghost"),)))
        with pytest.raises(MigrationError, match="does not match"):
            MigrationGraph({("gr_auth", "wrong"): a1})

    def _legacy_apps(self, dep: str) -> tuple:
        squashed = _script("e1", None) + "bedrock_squash = {'format': 1, 'old': [('x1', []), ('e1', ['gr_a:x1'])]}\n"
        return (
            ("gr_a", {"e1": squashed}, ()),
            ("gr_b", {"x1": _script("x1", None), "b2": _script("b2", "x1")}, ()),
            ("gr_c", {"c1": _script("c1", None, (dep,))}, ("gr_a", "gr_b")),
        )

    def _load_legacy(self, module_factory: ModuleFactory, *apps: tuple) -> MigrationGraph:
        registry = ModuleRegistry()
        registry.populate([_chain_app(module_factory, *a) for a in apps])
        return MigrationGraph.load(registry, legacy=True)

    def test_legacy_bare_dep_on_retired_name_live_elsewhere_is_refused(self, module_factory: ModuleFactory) -> None:
        with pytest.raises(MigrationError, match="retired by a squash in gr_a"):
            self._load_legacy(module_factory, *self._legacy_apps("x1"))

    def test_legacy_bare_dep_on_independent_name_still_resolves(self, module_factory: ModuleFactory) -> None:
        graph = self._load_legacy(module_factory, *self._legacy_apps("b2"))
        assert graph[("gr_c", "c1")].depends_on == (("gr_b", "b2"),)
