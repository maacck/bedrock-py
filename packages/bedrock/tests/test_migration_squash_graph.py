from collections.abc import Callable

import pytest
from bedrock.database.migration.graph import MigrationError, MigrationGraph
from bedrock.database.migration.squash import SquashSpec, build_spec, installed_specs
from bedrock.module.registry import ModuleRegistry
from bedrock.testing.modules import ModuleFactory


def _script(rev: str, down: str | None, deps: tuple[str, ...] = (), squash: object = None) -> str:
    src = (
        "from alembic import op\nimport sqlalchemy as sa\n"
        f"revision = {rev!r}\ndown_revision = {down!r}\ndepends_on = {deps!r}\n"
        f"def upgrade():\n    op.create_table({rev!r}, sa.Column('id', sa.Integer, primary_key=True))\n"
        f"def downgrade():\n    op.drop_table({rev!r})\n"
    )
    return src + (f"bedrock_squash = {squash!r}\n" if squash is not None else "")


def _chain(*revs: str, **deps: tuple[str, ...]) -> dict[str, str]:
    """Linear scripts: each of *revs* has the previous as down_revision; ``deps[rev]`` are its depends_on."""
    return {r: _script(r, revs[i - 1] if i else None, deps.get(r, ())) for i, r in enumerate(revs)}


ORDERS = ("0007_base", "0008_add", "0009_fill", "0010_finalize", "0011_ship")


@pytest.fixture
def load(module_factory: ModuleFactory) -> Callable[..., MigrationGraph]:
    def _load(*apps: tuple[str, dict[str, str], tuple[str, ...]]) -> MigrationGraph:
        names = [
            module_factory(
                name,
                manifest={"title": name, "version": "0.1.0", "depends_on": list(manifest_deps)},
                models="",
                files={f"migrations/{stem}.py": src for stem, src in scripts.items()},
            )
            for name, scripts, manifest_deps in apps
        ]
        registry = ModuleRegistry()
        registry.populate(names)
        return MigrationGraph.load(registry)

    return _load


def _orders(load: Callable[..., MigrationGraph], **deps: tuple[str, ...]) -> MigrationGraph:
    return load(("sq_orders", _chain(*ORDERS, **deps), ()))


def _published(**overrides: str) -> tuple[str, dict[str, str], tuple[str, ...]]:
    """sq_orders after squashing 0008..0010 in place."""
    squash = {
        "format": 1,
        "old": [
            ("0008_add", ["sq_orders:0007_base"]),
            ("0009_fill", ["sq_orders:0008_add"]),
            ("0010_finalize", ["sq_orders:0009_fill"]),
        ],
    }
    scripts = {
        "0007_base": _script("0007_base", None),
        "0010_finalize": _script("0010_finalize", "0007_base", squash=squash),
        "0011_ship": _script("0011_ship", "0010_finalize"),
    }
    return "sq_orders", {**scripts, **overrides}, ()


class TestBuildSpec:
    def test_tail_range_keeps_end_and_round_trips(self, load: Callable[..., MigrationGraph]) -> None:
        spec = build_spec(_orders(load), "sq_orders", "0008_add", "0010_finalize")

        assert spec.end == ("sq_orders", "0010_finalize")
        assert spec.keys == tuple(("sq_orders", r) for r in ("0008_add", "0009_fill", "0010_finalize"))
        assert spec.retired == spec.keys[:2]
        assert spec.parents == (("sq_orders", "0007_base"),)
        assert spec.metadata() == {
            "format": 1,
            "old": [
                ("0008_add", ["sq_orders:0007_base"]),
                ("0009_fill", ["sq_orders:0008_add"]),
                ("0010_finalize", ["sq_orders:0009_fill"]),
            ],
        }

    def test_root_range_has_no_parents(self, load: Callable[..., MigrationGraph]) -> None:
        assert build_spec(_orders(load), "sq_orders", "0007_base", "0009_fill").parents == ()

    def test_unknown_or_unchained_endpoints_rejected(self, load: Callable[..., MigrationGraph]) -> None:
        graph = _orders(load)
        for first, through in [("0008_add", "9999_nope"), ("0010_finalize", "0008_add"), ("0009_fill", "0009_fill")]:
            with pytest.raises(MigrationError):
                build_spec(graph, "sq_orders", first, through)

    def test_external_parents_are_unioned_when_already_ancestors(self, load: Callable[..., MigrationGraph]) -> None:
        users = ("sq_users", _chain("0001_init", "0002_more"), ())
        graph = load(
            users,
            (
                "sq_orders",
                _chain(*ORDERS, **{"0008_add": ("sq_users:0002_more",), "0009_fill": ("sq_users:0001_init",)}),
                ("sq_users",),
            ),
        )

        spec = build_spec(graph, "sq_orders", "0008_add", "0010_finalize")

        assert spec.parents == (("sq_orders", "0007_base"), ("sq_users", "0001_init"), ("sq_users", "0002_more"))

    def test_dependency_that_would_move_earlier_rejected(self, load: Callable[..., MigrationGraph]) -> None:
        graph = load(
            ("sq_users", _chain("0001_init"), ()),
            ("sq_orders", _chain(*ORDERS, **{"0009_fill": ("sq_users:0001_init",)}), ("sq_users",)),
        )

        with pytest.raises(MigrationError, match="move earlier"):
            build_spec(graph, "sq_orders", "0008_add", "0010_finalize")

    def test_nested_squash_rejected(self, load: Callable[..., MigrationGraph]) -> None:
        with pytest.raises(MigrationError, match="already a squashed"):
            build_spec(load(_published()), "sq_orders", "0007_base", "0010_finalize")


class TestReverseDependencies:
    def _billing(self, load: Callable[..., MigrationGraph], target: str) -> MigrationGraph:
        return load(
            ("sq_orders", _chain(*ORDERS), ()),
            ("sq_billing", _chain("0003_invoice", **{"0003_invoice": (f"sq_orders:{target}",)}), ("sq_orders",)),
        )

    def test_unapplied_dependent_on_middle_node_blocks(self, load: Callable[..., MigrationGraph]) -> None:
        graph = self._billing(load, "0009_fill")

        with pytest.raises(MigrationError) as err:
            build_spec(graph, "sq_orders", "0008_add", "0010_finalize")

        assert "sq_billing:0003_invoice" in err.value.detail
        assert "depends_on -> sq_orders:0009_fill" in err.value.detail

    def test_dependent_on_end_is_allowed(self, load: Callable[..., MigrationGraph]) -> None:
        spec = build_spec(self._billing(load, "0010_finalize"), "sq_orders", "0008_add", "0010_finalize")

        assert spec.end == ("sq_orders", "0010_finalize")

    def test_same_revision_name_in_other_app_does_not_block(self, load: Callable[..., MigrationGraph]) -> None:
        graph = load(
            ("sq_orders", _chain(*ORDERS), ()),
            ("sq_other", _chain("0009_fill", "0010_finalize"), ()),
        )

        assert build_spec(graph, "sq_orders", "0008_add", "0010_finalize").end[0] == "sq_orders"

    def test_fork_inside_range_blocks(self, load: Callable[..., MigrationGraph]) -> None:
        scripts = {**_chain(*ORDERS), "0009b_alt": _script("0009b_alt", "0008_add")}

        with pytest.raises(MigrationError, match="down_revision -> sq_orders:0008_add"):
            build_spec(load(("sq_orders", scripts, ())), "sq_orders", "0008_add", "0010_finalize")

    def test_successor_of_end_is_fine_but_blocks_when_renamed(self, load: Callable[..., MigrationGraph]) -> None:
        graph = _orders(load)

        assert build_spec(graph, "sq_orders", "0008_add", "0010_finalize")
        with pytest.raises(MigrationError, match="0011_ship"):
            build_spec(graph, "sq_orders", "0008_add", "0010_finalize", rename="0008_feature")


class TestRename:
    def test_rename_changes_end_and_retires_all(self, load: Callable[..., MigrationGraph]) -> None:
        graph = load(("sq_orders", _chain(*ORDERS[:4]), ()))

        spec = build_spec(graph, "sq_orders", "0008_add", "0010_finalize", rename="0008_feature")

        assert spec.end == ("sq_orders", "0008_feature")
        assert spec.retired == spec.keys

    @pytest.mark.parametrize("name", ["0007_base", "0009_fill", "bad:name", "../x", "", "x" * 65, "head", "base"])
    def test_collision_or_invalid_name_rejected(self, load: Callable[..., MigrationGraph], name: str) -> None:
        graph = load(("sq_orders", _chain(*ORDERS[:4]), ()))

        with pytest.raises(MigrationError):
            build_spec(graph, "sq_orders", "0008_add", "0010_finalize", rename=name)


class TestSquashSpec:
    A = tuple(("a", r) for r in ("1", "2", "3"))

    def _old(self, *, parents: tuple[tuple[str, str], ...] = ()) -> tuple:
        return ((self.A[0], parents), (self.A[1], (self.A[0],)), (self.A[2], (self.A[1],)))

    def test_valid(self) -> None:
        assert SquashSpec("a", self._old(), self.A[2], ()).keys == self.A

    @pytest.mark.parametrize(
        "make",
        [
            lambda s: SquashSpec("a", s._old()[:1], s.A[0], ()),
            lambda s: SquashSpec("a", (s._old()[0], s._old()[0]), s.A[0], ()),
            lambda s: SquashSpec("a", s._old(), ("a", "2"), ()),  # end may only reuse the last old node
            lambda s: SquashSpec("a", s._old(), ("b", "3"), ()),
            lambda s: SquashSpec("a", s._old(), s.A[2], (("b", "1"),)),  # parents differ from union
            lambda s: SquashSpec("a", (s._old()[0], (s.A[2], (s.A[0],)), s._old()[2]), s.A[2], ()),  # broken chain
            lambda s: SquashSpec("a", s._old(parents=(s.A[2],)), s.A[2], (s.A[2],)),  # first points into range
            lambda s: SquashSpec("a", (*s._old()[:2], (("a", "x:y"), (s.A[1],))), ("a", "x:y"), ()),
        ],
    )
    def test_invalid(self, make: Callable[["TestSquashSpec"], SquashSpec]) -> None:
        with pytest.raises(MigrationError):
            make(self)


class TestInstalledSpecs:
    def test_published_squash_loads_and_is_listed_even_when_fresh(self, load: Callable[..., MigrationGraph]) -> None:
        graph = load(_published())

        specs = installed_specs(graph)

        assert list(specs) == [("sq_orders", "0010_finalize")]
        assert specs[("sq_orders", "0010_finalize")].parents == (("sq_orders", "0007_base"),)

    def test_plain_graph_has_no_specs(self, load: Callable[..., MigrationGraph]) -> None:
        assert installed_specs(_orders(load)) == {}

    @pytest.mark.parametrize("stale", ["0008_add", "0009_fill"])
    def test_leftover_retired_file_rejected_on_load(self, load: Callable[..., MigrationGraph], stale: str) -> None:
        with pytest.raises(MigrationError, match=f"{stale}.*still exists"):
            load(_published(**{stale: _script(stale, "0007_base")}))

    def test_renamed_squash_retires_every_old_revision(self, load: Callable[..., MigrationGraph]) -> None:
        squash = {"format": 1, "old": [("0002_old", ["sq_ren:0001_base"]), ("0003_old", ["sq_ren:0002_old"])]}
        scripts = {"0001_base": _script("0001_base", None), "0002_new": _script("0002_new", "0001_base", squash=squash)}

        with pytest.raises(MigrationError, match="0003_old.*still exists"):
            load(("sq_ren", {**scripts, "0003_old": _script("0003_old", "0002_new")}, ()))

    def test_overlapping_descriptors_rejected(self, load: Callable[..., MigrationGraph]) -> None:
        other = {
            "format": 1,
            "old": [("0009_fill", ["sq_orders:0007_base"]), ("0012_x", ["sq_orders:0009_fill"])],
        }
        scripts = {
            "0007_base": _script("0007_base", None),
            "0010_finalize": _published()[1]["0010_finalize"],
            "0011_ship": _script("0011_ship", "0007_base", squash=other),
        }

        with pytest.raises(MigrationError, match="overlaps"):
            load(("sq_orders", scripts, ()))

    @pytest.mark.parametrize(
        "squash",
        [
            "nope",
            {"format": 2, "old": []},
            {"format": True, "old": []},
            {"format": 1, "old": [([], []), ("0010_finalize", [])]},
            {"format": 1},
            {"format": 1, "old": [("0008_add", ["sq_orders:0007_base"])]},  # one entry only
            {
                "format": 1,
                "old": [("0009_fill", ["0008_add"]), ("0010_finalize", ["sq_orders:0009_fill"])],
            },  # unqualified
            {"format": 1, "old": [("0009_fill", ["sq_orders:0007_base"]), ("0010_finalize", ["sq_orders:0008_add"])]},
        ],
    )
    def test_malformed_descriptor_rejected(self, load: Callable[..., MigrationGraph], squash: object) -> None:
        scripts = {
            "0007_base": _script("0007_base", None),
            "0010_finalize": _script("0010_finalize", "0007_base", squash=squash),
        }

        with pytest.raises(MigrationError, match="0010_finalize"):
            load(("sq_orders", scripts, ()))

    def test_end_parents_must_match_old_union(self, load: Callable[..., MigrationGraph]) -> None:
        squash = {
            "format": 1,
            "old": [("0008_add", ["sq_orders:0007_base"]), ("0010_finalize", ["sq_orders:0008_add"])],
        }
        scripts = {
            **_published()[1],
            "0006_x": _script("0006_x", None),
            "0010_finalize": _script("0010_finalize", "0006_x", squash=squash),
        }

        with pytest.raises(MigrationError, match="union"):
            load(("sq_orders", scripts, ()))

    def test_explicit_none_descriptor_rejected(self, load: Callable[..., MigrationGraph]) -> None:
        scripts = {
            "0007_base": _script("0007_base", None),
            "0010_finalize": _script("0010_finalize", "0007_base") + "bedrock_squash = None\n",
        }

        with pytest.raises(MigrationError, match="0010_finalize.*bedrock_squash must be"):
            load(("sq_orders", scripts, ()))

    def test_late_parent_outside_first_ancestors_rejected(self, load: Callable[..., MigrationGraph]) -> None:
        squash = {
            "format": 1,
            "old": [
                ("0008_add", ["sq_orders:0007_base"]),
                ("0009_fill", ["sq_orders:0008_add", "sq_users:0001_init"]),
            ],
        }
        scripts = {
            "0007_base": _script("0007_base", None),
            "0009_fill": _script("0009_fill", "0007_base", ("sq_users:0001_init",), squash=squash),
        }

        with pytest.raises(MigrationError, match="0009_fill.*move earlier.*sq_users:0001_init"):
            load(("sq_users", _chain("0001_init"), ()), ("sq_orders", scripts, ("sq_users",)))

    def test_late_parent_already_ancestor_of_first_accepted(self, load: Callable[..., MigrationGraph]) -> None:
        squash = {
            "format": 1,
            "old": [
                ("0008_add", ["sq_orders:0007_base"]),
                ("0009_fill", ["sq_orders:0008_add", "sq_users:0001_init"]),
            ],
        }
        scripts = {
            "0007_base": _script("0007_base", None, ("sq_users:0001_init",)),
            "0009_fill": _script("0009_fill", "0007_base", ("sq_users:0001_init",), squash=squash),
        }

        graph = load(("sq_users", _chain("0001_init"), ()), ("sq_orders", scripts, ("sq_users",)))

        assert list(installed_specs(graph)) == [("sq_orders", "0009_fill")]
