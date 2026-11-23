"""Dependency graph of every loaded module's migration scripts.

Nodes are ``(app import path, full revision name)`` pairs, so two modules may
both ship ``0001_initial``. Edges come from two places:

- ``down_revision``: a parent in the *same* app (never another app).
- ``depends_on``: a qualified ``"<app>:<revision>"`` reference to a migration of
  a direct manifest dependency.

Only legacy adoption resolves bare Alembic dependencies, and only when exactly one
script in the original deployment owns that name. Normal scripts must qualify them.

This file must not use ``from __future__ import annotations``.
"""

import graphlib
import heapq
import importlib.util
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

from ...exc import BedrockExc

if TYPE_CHECKING:
    from ...module.registry import ModuleRegistry

MigrationKey = tuple[str, str]  # (app import path, full revision name)

_MAX_REVISION_LENGTH = 64  # width of bedrock_migrations.revision


class MigrationError(BedrockExc, RuntimeError):
    """Raised when a migration operation cannot be completed.

    Subclasses ``RuntimeError`` so callers that caught the generic error keep working.

    Attributes:
        app_import_path: The import path of the app that triggered the error,
            if applicable.
    """

    detail: str = "Migration operation failed."

    def __init__(self, message: str, app_import_path: str | None = None) -> None:
        self.app_import_path = app_import_path
        super().__init__(msg=message)


@dataclass(frozen=True)
class Migration:
    """One migration script owned by a loaded module."""

    app: str
    revision: str
    down_revisions: tuple[MigrationKey, ...]  # resolved within the script's own app
    depends_on: tuple[MigrationKey, ...]  # parsed from qualified app:revision references
    message: str  # first line of the module docstring, "" when absent
    path: Path
    module: ModuleType  # exposes upgrade() and downgrade()

    @property
    def key(self) -> MigrationKey:
        return (self.app, self.revision)

    @property
    def parents(self) -> tuple[MigrationKey, ...]:
        """Every direct parent (``down_revisions`` plus ``depends_on``), unique and sorted."""
        return tuple(sorted({*self.down_revisions, *self.depends_on}))


def _strings(value: object, what: str, where: str, app: str) -> tuple[str, ...]:
    """Normalize ``None`` / ``str`` / list or tuple of ``str`` to a tuple of non-empty strings."""
    if value is None:
        return ()
    items = (value,) if isinstance(value, str) else value
    if not isinstance(items, (list, tuple)) or not all(isinstance(i, str) and i for i in items):
        raise MigrationError(f"{where}: {what} must be None, a string, or a list/tuple of strings.", app)
    return tuple(items)


def _load_script(app: str, path: Path, depends: set[str], legacy: bool) -> Migration:
    """Execute one script and return its migration; *depends* is the owner's direct manifest dependencies."""
    spec = importlib.util.spec_from_file_location(f"bedrock_migrations.{app}.{path.stem}", path)
    if spec is None or spec.loader is None:
        raise MigrationError(f"Cannot load migration script {path}.", app)
    module = importlib.util.module_from_spec(spec)
    try:
        # Do not use stale bytecode after a same-size script edit within one second.
        exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    except Exception as exc:
        raise MigrationError(f"Cannot load migration script {path}: {exc}", app) from exc
    revision = getattr(module, "revision", None)
    if not isinstance(revision, str) or not revision:
        raise MigrationError(f"Migration script {path} must define a string 'revision'.", app)
    if ":" in revision:
        raise MigrationError(f"Invalid ':' in revision name {revision!r} in {path}.", app)
    if len(revision) > _MAX_REVISION_LENGTH:
        raise MigrationError(f"Revision '{revision}' in {path} is longer than {_MAX_REVISION_LENGTH} characters.", app)
    where = f"{app}:{revision} ({path})"
    downs = _strings(getattr(module, "down_revision", None), "down_revision", where, app)
    deps = _strings(getattr(module, "depends_on", None), "depends_on", where, app)
    parsed: list[MigrationKey] = []
    for ref in deps:
        dep_app, sep, dep_rev = ref.partition(":")
        if legacy and not sep:
            parsed.append(("", ref))  # resolved after all owners are known
            continue
        if not sep or not dep_app or not dep_rev:
            raise MigrationError(
                f"{where}: depends_on entry {ref!r} must be a qualified 'app:revision' reference; "
                "bare revision names are ambiguous across modules.",
                app,
            )
        if dep_app not in depends:
            raise MigrationError(
                f"{where}: depends_on entry {ref!r} points to app '{dep_app}', which is not a direct "
                f"manifest dependency of '{app}'.",
                app,
            )
        parsed.append((dep_app, dep_rev))
    doc = (module.__doc__ or "").strip()
    return Migration(
        app=app,
        revision=revision,
        down_revisions=tuple((app, d) for d in downs),
        depends_on=tuple(parsed),
        message=doc.splitlines()[0] if doc else "",
        path=path,
        module=module,
    )


class MigrationGraph:
    """Validated dependency graph of every loaded module's migration scripts.

    Attributes:
        migrations: Every migration keyed by ``(app, revision)``.
    """

    def __init__(self, migrations: dict[MigrationKey, Migration]) -> None:
        """Validate edges and acyclicity.

        Raises:
            MigrationError: A key does not match its migration, a ``down_revision`` is not a
                revision of the same app, a ``depends_on`` target is unknown, or there is a cycle.
        """
        self.migrations = dict(migrations)
        self._children: dict[MigrationKey, set[MigrationKey]] = {key: set() for key in self.migrations}  # down_revision
        self._dependents: dict[MigrationKey, set[MigrationKey]] = {key: set() for key in self.migrations}  # any edge
        for key, mig in self.migrations.items():
            if key != mig.key:
                raise MigrationError(f"Graph key {key!r} does not match migration {mig.key!r}.", key[0])
            for down in mig.down_revisions:
                if down[0] != mig.app or down not in self.migrations:
                    raise MigrationError(
                        f"{mig.app}:{mig.revision} has down_revision '{down[1]}', "
                        f"which is not a revision of app '{mig.app}'.",
                        mig.app,
                    )
                self._children[down].add(key)
                self._dependents[down].add(key)
            for dep in mig.depends_on:
                if dep not in self.migrations:
                    raise MigrationError(
                        f"{mig.app}:{mig.revision} depends on unknown migration '{dep[0]}:{dep[1]}'.", mig.app
                    )
                self._dependents[dep].add(key)
        try:
            graphlib.TopologicalSorter({k: self._parents(k) for k in self.migrations}).prepare()
        except graphlib.CycleError as exc:
            cycle = " -> ".join(f"{a}:{r}" for a, r in exc.args[1])
            raise MigrationError(f"Migration dependency cycle: {cycle}.") from exc

    @classmethod
    def load(cls, registry: "ModuleRegistry", *, legacy: bool = False) -> "MigrationGraph":
        """Load ``<package_dir>/migrations/*.py`` of every loaded app and validate.

        Files whose name starts with ``_`` are skipped. A script without a string
        ``revision`` attribute raises MigrationError. ``branch_labels`` is ignored.

        Raises:
            MigrationError: load failure, duplicate (app, revision), down_revision that is
                not a revision of the same app, depends_on that is unknown, bare, or points to
                an app missing from the owner's manifest ``depends_on`` (direct only),
                or a dependency cycle.
        """
        migrations: dict[MigrationKey, Migration] = {}
        for app in sorted(registry.all(), key=lambda a: a.name):
            directory = app.package_dir / "migrations"
            try:
                paths = sorted(directory.iterdir())
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise MigrationError(f"Cannot list migrations in {directory}: {exc}", app.name) from exc
            depends = set(app.manifest.depends_on)
            for path in paths:
                if path.suffix != ".py" or path.name.startswith("_"):
                    continue
                mig = _load_script(app.name, path, depends, legacy)
                if mig.key in migrations:
                    raise MigrationError(
                        f"Duplicate revision '{mig.revision}' in app '{app.name}': "
                        f"{migrations[mig.key].path.name} and {path.name}.",
                        app.name,
                    )
                migrations[mig.key] = mig
        if legacy:
            retired = cls._retired(migrations.values())
            for key, migration in migrations.items():
                resolved = []
                scope = registry.get(key[0]).manifest.depends_on
                for owner, rev in migration.depends_on:
                    if not owner:
                        matches = [a for a, r in migrations if r == rev]
                        if gone := sorted(a for a, r in retired if r == rev and a in scope):
                            raise MigrationError(
                                f"Legacy dependency {rev!r} of {key} was retired by a squash in {', '.join(gone)} "
                                f"but is still live in {matches!r}; it cannot be attributed. Qualify it "
                                "on the bridge release before squashing."
                            )
                        if len(matches) != 1 or matches[0] not in scope:
                            raise MigrationError(f"Cannot resolve legacy dependency {rev!r} of {key}: {matches!r}.")
                        owner = matches[0]
                    resolved.append((owner, rev))
                migrations[key] = replace(migration, depends_on=tuple(resolved))
        graph = cls(migrations)
        from .squash import installed_specs  # squash imports this module

        installed_specs(graph)  # published squash descriptors must be consistent, even on a fresh database
        return graph

    @staticmethod
    def _retired(migrations: Iterable[Migration]) -> set[MigrationKey]:
        """Names replaced by an installed squash descriptor, read leniently (malformed ones fail later)."""
        gone: set[MigrationKey] = set()
        for mig in migrations:
            raw = getattr(mig.module, "bedrock_squash", None)
            items = raw.get("old") if isinstance(raw, dict) else None
            for item in items if isinstance(items, (list, tuple)) else ():
                if isinstance(item, (list, tuple)) and item and isinstance(item[0], str) and item[0] != mig.revision:
                    gone.add((mig.app, item[0]))
        return gone

    def retired(self) -> set[MigrationKey]:
        """Keys of old revisions that installed squashes replaced (their scripts are gone)."""
        return self._retired(self.migrations.values())

    def __contains__(self, key: object) -> bool:
        return key in self.migrations

    def __getitem__(self, key: MigrationKey) -> Migration:
        return self.migrations[key]

    def revisions(self, app: str) -> set[MigrationKey]:
        """Every migration key owned by *app*."""
        return {key for key in self.migrations if key[0] == app}

    def heads(self, app: str) -> tuple[MigrationKey, ...]:
        """Sorted keys of *app* that no other migration of *app* names as ``down_revision``."""
        return tuple(sorted(key for key in self.revisions(app) if not self._children[key]))

    def ancestors(self, revisions: Iterable[MigrationKey]) -> set[MigrationKey]:
        """Inclusive closure over ``down_revision`` and ``depends_on`` edges."""
        return self._closure(revisions, self._parents)

    def dependents(self, key: MigrationKey) -> tuple[MigrationKey, ...]:
        """Sorted migrations naming *key* in ``down_revision`` or ``depends_on``."""
        return tuple(sorted(self._dependents[key]))

    def descendants_in_app(self, revisions: Iterable[MigrationKey]) -> set[MigrationKey]:
        """Inclusive closure over same-app ``down_revision`` children; cross-app dependents are excluded."""
        return self._closure(revisions, self._children.__getitem__)

    def order(self, revisions: Iterable[MigrationKey]) -> list[MigrationKey]:
        """Topologically sort *revisions* (parents first), smallest key first among ties.

        Only edges between the given keys count; missing ancestors are not added.
        """
        subset = self._known(revisions)
        pending = {key: len(self._parents(key) & subset) for key in subset}
        ready = [key for key, count in pending.items() if not count]
        heapq.heapify(ready)
        result: list[MigrationKey] = []
        while ready:
            key = heapq.heappop(ready)
            result.append(key)
            for child in self._dependents[key] & subset:
                pending[child] -= 1
                if not pending[child]:
                    heapq.heappush(ready, child)
        return result

    def _parents(self, key: MigrationKey) -> set[MigrationKey]:
        return set(self.migrations[key].parents)

    def _known(self, revisions: Iterable[MigrationKey]) -> set[MigrationKey]:
        keys = set(revisions)
        unknown = sorted(keys - self.migrations.keys())
        if unknown:
            raise MigrationError(f"Unknown migrations: {', '.join(f'{a}:{r}' for a, r in unknown)}.")
        return keys

    def _closure(
        self, revisions: Iterable[MigrationKey], step: Callable[[MigrationKey], set[MigrationKey]]
    ) -> set[MigrationKey]:
        seen = self._known(revisions)
        stack = list(seen)
        while stack:
            for nxt in step(stack.pop()) - seen:
                seen.add(nxt)
                stack.append(nxt)
        return seen
