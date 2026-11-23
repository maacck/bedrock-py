"""Squash replacement descriptors and the graph-level checks around them.

A squashed script declares which old revisions of its app it replaces::

    bedrock_squash = {
        "format": 1,
        "old": [("0008_add", ["orders:0007_base"]), ...],
    }

``old`` lists the replaced revisions in chain order with the full parent snapshot each had.
The published script's own ``revision`` is the new end ``E``; it equals the last old revision
unless the author renamed it. Everything here is pure graph logic: no database, no file writes.
"""

import re
from dataclasses import dataclass, replace

from .graph import _MAX_REVISION_LENGTH, Migration, MigrationError, MigrationGraph, MigrationKey

FORMAT = 1
_RENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_]*")  # the loader skips files beginning with '_'
_RESERVED = {"head", "base"}  # Alembic revision selectors


def _label(key: MigrationKey) -> str:
    return f"{key[0]}:{key[1]}"


def _name_ok(name: object) -> bool:
    return isinstance(name, str) and 0 < len(name) <= _MAX_REVISION_LENGTH and ":" not in name


def _outward(old: tuple[tuple[MigrationKey, tuple[MigrationKey, ...]], ...]) -> list[set[MigrationKey]]:
    """Per old node, the parents that are not its predecessor in the chain."""
    return [set(parents) - ({old[i - 1][0]} if i else set()) for i, (_, parents) in enumerate(old)]


@dataclass(frozen=True)
class SquashSpec:
    """Immutable description of one squashed range of a single app.

    Attributes:
        app: Owning app import path.
        old: Replaced ``(key, parents snapshot)`` pairs in chain order; the last key is the old end ``O``.
        end: Key of the published script ``E`` (``O`` unless renamed).
        parents: Sorted parent snapshot of ``E``: the union of every old node's parents outside the chain.
    """

    app: str
    old: tuple[tuple[MigrationKey, tuple[MigrationKey, ...]], ...]
    end: MigrationKey
    parents: tuple[MigrationKey, ...]

    def __post_init__(self) -> None:
        def bad(msg: str) -> MigrationError:
            return MigrationError(f"Invalid squash of '{self.app}': {msg}", self.app)

        keys = self.keys
        if len(keys) < 2:
            raise bad("at least two old revisions are required.")
        if len(set(keys)) != len(keys):
            raise bad("duplicate old revisions.")
        if any(k[0] != self.app or not _name_ok(k[1]) for k in (*keys, self.end)):
            raise bad("revisions must be valid names of the squashed app.")
        if self.end in keys and self.end != keys[-1]:
            raise bad(f"end '{self.end[1]}' may only reuse the last old revision.")
        for i, (key, parents) in enumerate(self.old):
            same = {p for p in parents if p[0] == self.app}
            if i and same != {keys[i - 1]}:
                raise bad(f"'{key[1]}' must have exactly the previous revision as its only same-app parent.")
            if not i and (len(same) > 1 or same & set(keys)):
                raise bad(f"first revision '{key[1]}' must have at most one same-app parent outside the range.")
        expected = tuple(sorted(set().union(*_outward(self.old))))
        if self.parents != expected:
            raise bad("end parents must be the union of the old parents outside the range.")

    @property
    def keys(self) -> tuple[MigrationKey, ...]:
        """Old keys in chain order."""
        return tuple(key for key, _ in self.old)

    @property
    def retired(self) -> tuple[MigrationKey, ...]:
        """Old keys that no longer exist once ``E`` is published."""
        return tuple(key for key in self.keys if key != self.end)

    def metadata(self) -> dict[str, object]:
        """The literal stored as ``bedrock_squash`` in the published script."""
        return {
            "format": FORMAT,
            "old": [(key[1], [_label(p) for p in parents]) for key, parents in self.old],
        }


def _ref(ref: object, where: str, app: str) -> MigrationKey:
    owner, sep, rev = ref.partition(":") if isinstance(ref, str) else ("", "", "")
    if not sep or not owner or not _name_ok(rev):
        raise MigrationError(f"{where}: bedrock_squash parent {ref!r} must be an 'app:revision' string.", app)
    return (owner, rev)


def _spec_of(mig: Migration) -> SquashSpec | None:
    if not hasattr(mig.module, "bedrock_squash"):
        return None
    raw = mig.module.bedrock_squash
    where = f"{mig.app}:{mig.revision} ({mig.path})"
    if (
        not isinstance(raw, dict)
        or set(raw) != {"format", "old"}
        or type(raw["format"]) is not int
        or raw["format"] != FORMAT
    ):
        raise MigrationError(f"{where}: bedrock_squash must be {{'format': {FORMAT}, 'old': [...]}}.", mig.app)
    items = raw["old"]
    if not isinstance(items, (list, tuple)) or not all(
        isinstance(i, (list, tuple)) and len(i) == 2 and _name_ok(i[0]) and isinstance(i[1], (list, tuple))
        for i in items
    ):
        raise MigrationError(f"{where}: bedrock_squash 'old' must be a list of (revision, [parents]).", mig.app)
    old = tuple(((mig.app, rev), tuple(sorted({_ref(p, where, mig.app) for p in ps}))) for rev, ps in items)
    try:
        return SquashSpec(mig.app, old, mig.key, mig.parents)
    except MigrationError as exc:
        raise MigrationError(f"{where}: {exc.detail}", mig.app) from exc


def installed_specs(graph: MigrationGraph) -> dict[MigrationKey, SquashSpec]:
    """Parse and validate every ``bedrock_squash`` in *graph*, keyed by the spec's end.

    Raises:
        MigrationError: malformed descriptor, descriptors that overlap, or a retired revision
            whose script is still present (even when no database was ever touched).
    """
    specs: dict[MigrationKey, SquashSpec] = {}
    for key, mig in graph.migrations.items():
        spec = _spec_of(mig)
        if spec is not None:
            specs[key] = spec
    owner: dict[MigrationKey, MigrationKey] = {}
    for end, spec in specs.items():
        for key in {*spec.keys, end}:
            if owner.setdefault(key, end) != end:
                raise MigrationError(
                    f"Squash of {_label(end)} overlaps squash of {_label(owner[key])} at {_label(key)}.", end[0]
                )
    for end, spec in specs.items():
        for key in spec.retired:
            if key in graph:
                raise MigrationError(
                    f"{_label(key)} was replaced by squashed {_label(end)} but its script "
                    f"{graph[key].path} still exists; delete the retired files.",
                    end[0],
                )
    for end, spec in specs.items():
        # parents are all in the graph (it validated the end's parents, a superset of the first node's)
        moved = sorted(set().union(*_outward(spec.old)[1:]) - graph.ancestors(spec.old[0][1]))
        if moved:
            raise MigrationError(
                f"{_label(end)} ({graph[end].path}): bedrock_squash late parents would move earlier, before "
                f"{_label(spec.keys[0])}: {', '.join(map(_label, moved))}.",
                end[0],
            )
    return specs


def build_spec(graph: MigrationGraph, app: str, first: str, through: str, rename: str | None = None) -> SquashSpec:
    """Describe squashing ``first..through`` of *app*; the end keeps ``through`` unless *rename* is given.

    The range is the single-parent same-app chain from *through* back to *first*. It is rejected
    when it is shorter than two, nests an installed squash, or has a revision outside the range
    that names a retired revision in ``down_revision`` or ``depends_on`` (the end itself may be
    referenced unless renamed). Parents of later nodes must already be ancestors of the first node,
    and the replaced graph must stay valid. Nothing is rewritten for callers.

    Raises:
        MigrationError: any rule above is violated.
    """
    start, stop = (app, first), (app, through)
    for key in (start, stop):
        if key not in graph:
            raise MigrationError(f"Unknown migration {_label(key)}.", app)
    chain = [stop]
    while chain[-1] != start:
        downs = graph[chain[-1]].down_revisions
        if len(downs) != 1:
            raise MigrationError(
                f"{_label(stop)} is not a single-parent chain back to {_label(start)}: "
                f"{_label(chain[-1])} has {len(downs)} down_revisions.",
                app,
            )
        chain.append(downs[0])
    chain.reverse()
    if len(chain) < 2:
        raise MigrationError("A squash needs at least two revisions.", app)

    members = set(chain)
    specs = installed_specs(graph)
    if nested := sorted(members & specs.keys()):
        raise MigrationError(f"{_label(nested[0])} is already a squashed revision; it cannot be squashed again.", app)
    end = stop
    if rename is not None:
        if not _RENAME.fullmatch(rename) or len(rename) > _MAX_REVISION_LENGTH or rename in _RESERVED:
            raise MigrationError(f"Invalid new revision name {rename!r}.", app)
        end = (app, rename)
        taken = end in graph or any(end in spec.keys for spec in specs.values())
        if taken:
            raise MigrationError(f"New revision name {rename!r} is already used or retired.", app)

    blocked = []
    for key in chain if rename is not None else chain[:-1]:
        for dep in graph.dependents(key):
            if dep not in members:
                kind = "down_revision" if key in graph[dep].down_revisions else "depends_on"
                blocked.append(f"{_label(dep)} ({graph[dep].path}) {kind} -> {_label(key)}")
    if blocked:
        raise MigrationError("Cannot retire revisions still referenced outside the range:\n" + "\n".join(blocked), app)

    old = tuple((key, graph[key].parents) for key in chain)
    parents = tuple(sorted(set().union(*_outward(old))))
    reachable = graph.ancestors(graph[start].parents)
    moved = sorted(set().union(*_outward(old)[1:]) - reachable)
    if moved:
        raise MigrationError(
            "Dependencies of later revisions would move earlier, before "
            f"{_label(start)}: {', '.join(map(_label, moved))}.",
            app,
        )
    spec = SquashSpec(app, old, end, parents)

    merged = {key: mig for key, mig in graph.migrations.items() if key not in members}
    merged[end] = replace(
        graph[stop],
        revision=end[1],
        down_revisions=tuple(p for p in parents if p[0] == app),
        depends_on=tuple(p for p in parents if p[0] != app),
    )
    MigrationGraph(merged)  # raises on unknown targets or cycles
    return spec
