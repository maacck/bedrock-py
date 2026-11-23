"""Plan module-scoped schema changes and execute their Alembic operations."""

import math
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypedDict
from urllib.parse import quote, unquote, urlsplit

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, Engine, Table, create_engine, text
from sqlalchemy.engine.url import URL, make_url
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.pool import NullPool

from ...exc import BedrockExc
from ...logging import get_logger
from . import ledger, squash, squash_writer
from .graph import MigrationError, MigrationGraph, MigrationKey
from .writer import write_revision

if TYPE_CHECKING:
    from ...module.entities import AppConfig
    from ...module.registry import ModuleRegistry

# Stable signed key that fits in PostgreSQL's bigint advisory-lock argument.
_POSTGRES_MIGRATION_LOCK_KEY = 0x424544524F434B
_MYSQL_MIGRATION_LOCK_NAME = "bedrock_migrations"

log = get_logger(__name__)

_DATABASE_SELECTORS = frozenset({"dbname", "database", "db"})
_PYSQLITE_CONNECT_ARGS = frozenset(
    {"uri", "timeout", "isolation_level", "detect_types", "check_same_thread", "cached_statements"}
)


class _MissingDatabase(BedrockExc):
    """A noncreating open found no SQLite file."""

    detail = "SQLite database file does not exist."


class _LockResult(Protocol):
    """Result shape used by the migration lock statements."""

    def scalar(self) -> object:
        """Return the first column of the first row."""


class _LockConnection(Protocol):
    """Connection shape used to acquire and release a migration lock."""

    def execute(self, statement: object, parameters: dict[str, object] | None = None) -> _LockResult:
        """Execute one lock statement."""

    def commit(self) -> None:
        """Finish the transaction that carried the lock statement."""


@dataclass(frozen=True)
class BranchRevisionStatus:
    """Current and head revisions for one module.

    Attributes:
        branch: The module import path.
        current: Applied revision ids. Empty when the branch has not been applied.
        head: Head revision ids from the migration scripts.
        state: ``at-head``, ``behind``, or ``unknown``.
    """

    branch: str
    current: tuple[str, ...]
    head: tuple[str, ...]
    state: str


class RevisionHistoryEntry(TypedDict):
    """One revision of a branch as returned by :meth:`MigrationsManager.get_history`."""

    revision: str
    down_revision: str
    depends_on: str
    message: str
    is_head: bool


@dataclass(frozen=True)
class SquashReport:
    """Outcome of :meth:`MigrationsManager.apply_squash`.

    Attributes:
        target: Database URL without credentials or query string.
        version_table: Ledger table that was read (and written when ``applied``).
        key: The named replacement's end revision.
        state: ``fresh``, ``current`` or ``replace``, classified before any write.
        applied: Whether ledger changes were committed.
        update: ``(old end, new end)`` row rewrite for ``replace``, else ``None``.
        delete: Exact ledger rows removed for ``replace``, else empty.
        states: Preview state of every installed replacement, by end key.
        remaining: Other replacements that still need their own ``apply_squash``.
    """

    target: str
    version_table: str
    key: MigrationKey
    state: str
    applied: bool
    update: tuple[MigrationKey, MigrationKey] | None
    delete: tuple[MigrationKey, ...]
    states: dict[MigrationKey, str]
    remaining: tuple[MigrationKey, ...]


def _key_names(keys: set[MigrationKey]) -> str:
    return ", ".join(f"{app}:{rev}" for app, rev in sorted(keys))


def _rollback_steps(
    graph: MigrationGraph,
    applied: dict[MigrationKey, ledger.AppliedMigration],
    removing: set[MigrationKey],
    app: str,
    cascade: bool,
) -> list[MigrationKey]:
    """Preflight the entire reverse closure, including rows from absent modules."""
    dependents: dict[MigrationKey, set[MigrationKey]] = defaultdict(set)
    for key, row in applied.items():
        for parent in row.parents:
            dependents[parent].add(key)
    pending = list(removing)
    while pending:
        more = dependents[pending.pop()] - removing
        removing.update(more)
        pending.extend(more)
    unloaded = {key for key in removing if key not in graph}
    if unloaded:
        raise MigrationError(
            f"Dependent migrations belong to unloaded modules: {_key_names(unloaded)}. "
            "Load them and roll them back first.",
            app,
        )
    baseline = {key for key in removing if applied[key].origin == "baseline"}
    if baseline:
        raise MigrationError(f"Cannot remove protected baseline migrations: {_key_names(baseline)}.", app)
    foreign = {key for key in removing if key[0] != app}
    if foreign and not cascade:
        raise MigrationError(
            f"Applied migrations depend on this rollback: {_key_names(foreign)}. "
            "Roll them back first or use --cascade.",
            app,
        )
    return list(reversed(graph.order(removing)))


class MigrationsManager:
    """Run migrations belonging to a populated Bedrock module registry.

    Args:
        registry: Loaded modules and their declared dependencies.
        database_url: SQLAlchemy URL for the database being managed.
        lock_timeout: Maximum seconds to wait for the database migration lock.
        version_table: Ledger name for this registry. Use a distinct name only for a disjoint schema owner.
    """

    def __init__(
        self,
        registry: "ModuleRegistry",
        database_url: str,
        lock_timeout: float = 30,
        *,
        version_table: str = ledger.VERSION_TABLE_NAME,
    ) -> None:
        if lock_timeout < 0:
            raise MigrationError(f"Migration lock timeout must be nonnegative (got {lock_timeout}).")
        self._registry = registry
        self._database_url = database_url
        self._lock_timeout = lock_timeout
        self._ledger = ledger.MigrationLedger(version_table)

    @property
    def version_table(self) -> str:
        """Name of this manager's migration ledger in the connection's default schema."""
        return self._ledger.name

    @contextmanager
    def _engine(self) -> Iterator[Engine]:
        """Keep pysqlite DDL inside real transactions, including first-install DDL."""
        url = make_url(self._database_url)
        args = {"autocommit": False} if url.get_driver_name() == "pysqlite" else {}
        engine = create_engine(url, connect_args=args)
        try:
            yield engine
        except SQLAlchemyError as exc:
            raise MigrationError(f"Migration database operation failed: {exc}") from exc
        finally:
            engine.dispose()

    def _get_app(self, name: str) -> "AppConfig":
        try:
            app = self._registry.get(name)
        except KeyError as exc:
            raise MigrationError(f"App '{name}' is not loaded.", name) from exc
        if app.models_module is None:
            raise MigrationError(f"App '{name}' has no models submodule.", name)
        return app

    def _validate_app(
        self,
        name: str,
        graph: MigrationGraph,
        applied: dict[MigrationKey, ledger.AppliedMigration],
        *,
        ignore: frozenset[MigrationKey] = frozenset(),
    ) -> None:
        for key, row in applied.items():
            if key[0] != name or key in ignore:
                continue
            if key not in graph:
                raise MigrationError(
                    f"Applied revision '{name}:{key[1]}' has no migration script; restore it before migrating.", name
                )
            script = graph[key]
            if row.parents != script.parents:
                raise MigrationError(
                    f"Dependency drift for '{name}:{key[1]}': recorded {row.parents!r}, "
                    f"script declares {script.parents!r}.",
                    name,
                )
            missing = set(row.parents) - applied.keys()
            if missing:
                raise MigrationError(
                    f"Applied revision '{name}:{key[1]}' is missing dependencies: {_key_names(missing)}.", name
                )

    def _state(self, engine: Engine, graph: MigrationGraph) -> dict[MigrationKey, ledger.AppliedMigration]:
        with engine.connect() as conn:
            applied = self._ledger.read_applied(conn)
        for app in self._registry.all():
            self._validate_app(app.name, graph, applied)
        return applied

    def revision(self, app_import_path: str, message: str, *, autogenerate: bool = True) -> Path:
        """Write a new module-local script without changing the database."""
        app = self._get_app(app_import_path)
        if not autogenerate:
            return write_revision(app, MigrationGraph.load(self._registry), message, None, None)
        with self._migration_lock(), self._engine() as engine:
            graph = MigrationGraph.load(self._registry)
            applied = self._state(engine, graph)
            if graph.revisions(app.name) - applied.keys():
                raise MigrationError(
                    f"'{app.name}' is not at head; run 'bedrock db upgrade {app.name}' first.", app.name
                )
            owners = self._resolve_table_owners()
            with engine.connect() as conn:
                return write_revision(app, graph, message, conn, owners, version_table=self.version_table)

    def _run_steps(self, engine: Engine, graph: MigrationGraph, steps: list[MigrationKey], *, upgrade: bool) -> None:
        if not steps:
            return
        if upgrade:
            with engine.begin() as conn:
                self._ledger.ensure_table(conn)
        for key in steps:
            migration = graph[key]
            try:
                with engine.begin() as conn:
                    with Operations.context(MigrationContext.configure(conn)):
                        if upgrade:
                            migration.module.upgrade()
                        else:
                            migration.module.downgrade()
                    if upgrade:
                        self._ledger.record(conn, migration)
                    else:
                        self._ledger.forget(conn, key)
            except Exception as exc:
                raise MigrationError(
                    f"Migration '{key[0]}:{key[1]}' failed: {exc}. "
                    "Inspect schema and data before retrying, especially on nontransactional DDL backends.",
                    key[0],
                ) from exc

    def _upgrade_scope(
        self, graph: MigrationGraph, name: str, target: str, applied: set[MigrationKey]
    ) -> set[MigrationKey]:
        local = graph.revisions(name)
        if not local:
            raise MigrationError(
                f"App '{name}' has models but no migration scripts; "
                f"run 'bedrock db revision {name} -m \"initial schema\"' first.",
                name,
            )
        if target == "head":
            targets = list(graph.heads(name))
        elif re.fullmatch(r"\+[1-9]\d*", target):
            count = int(target)
            pending = [key for key in graph.order(local) if key not in applied]
            if count > len(pending):
                raise MigrationError(f"Not enough pending migrations of '{name}' for '{target}'.", name)
            targets = pending[:count]
        elif (name, target) in local:
            targets = [(name, target)]
        else:
            raise MigrationError(f"Target '{target}' is not a revision of '{name}'.", name)
        return graph.ancestors(targets)

    def _upgrade_snapshot(
        self,
        conn: Connection,
        graph: MigrationGraph,
        name: str,
        target: str,
        specs: dict[MigrationKey, squash.SquashSpec],
    ) -> tuple[set[MigrationKey], dict[MigrationKey, ledger.AppliedMigration], list[squash.SquashSpec]]:
        recorded = self._ledger.read_applied(conn)
        # Relative targets count a completed renamed range once. Its eligibility is
        # checked below if the selected target actually depends on it.
        known = set(recorded)
        if target.startswith("+"):
            known.update(spec.end for spec in specs.values() if all(k in recorded for k in spec.keys))
        scope = self._upgrade_scope(graph, name, target, known)
        selected = {key: specs[key] for key in sorted(scope & specs.keys())}
        # Pass the entire ledger: a row in an unloaded app can still refer to a retired key.
        states, projected = ledger.project_squashes(recorded, selected)
        unrelated = frozenset(k for key, spec in specs.items() if key not in selected for k in (*spec.keys, key))
        for app in {app for app, _ in scope}:
            self._validate_app(app, graph, projected, ignore=unrelated)
        return scope, projected, [spec for key, spec in selected.items() if states[key] == "replace"]

    def _upgrade(self, name: str, target: str) -> tuple[list[MigrationKey], bool]:
        self._get_app(name)
        graph = MigrationGraph.load(self._registry)
        specs = squash.installed_specs(graph)
        if specs:
            # Reconcile in a ledger-only transaction before taking the normal migration lock.
            # Never acquire the same database lock recursively on another connection.
            with self._squash_transaction() as conn:
                _, _, pending = self._upgrade_snapshot(conn, graph, name, target, specs)
                if pending:
                    self._check_squash_storage(conn, self._backend(), "replace")
                    for spec in pending:
                        self._ledger.replace_squash(conn, spec)
                self._squash_end(conn, self._backend(), commit=bool(pending))

        with self._migration_lock(), self._engine() as engine:
            if specs:
                # Another migration may have completed between locks. Plan from a fresh snapshot.
                with engine.connect() as conn:
                    scope, applied, pending = self._upgrade_snapshot(conn, graph, name, target, specs)
                if pending:
                    raise MigrationError("Ledger changed during squash reconciliation; retry the upgrade.", name)
            else:
                applied = self._state(engine, graph)
                scope = self._upgrade_scope(graph, name, target, set(applied))
            steps = graph.order(scope - applied.keys())
            self._run_steps(engine, graph, steps, upgrade=True)
            return steps, not bool(graph.revisions(name) & applied.keys())

    def upgrade(self, app_import_path: str, target: str = "head") -> list[MigrationKey]:
        """Apply the target and its pending ancestors, returning the executed steps."""
        return self._upgrade(app_import_path, target)[0]

    def ensure_schema(self, app_import_path: str) -> str:
        """Run scripts even on first install; return created, upgraded or up-to-date."""
        steps, first = self._upgrade(app_import_path, "head")
        if not steps:
            return "up-to-date"
        return "created" if first else "upgraded"

    def downgrade(self, app_import_path: str, target: str, *, cascade: bool = False) -> list[MigrationKey]:
        """Revert applied descendants after preflighting dependencies and baseline protection."""
        app = self._get_app(app_import_path)
        with self._migration_lock(), self._engine() as engine:
            graph = MigrationGraph.load(self._registry)
            applied = self._state(engine, graph)
            local = graph.revisions(app.name) & applied.keys()
            if target == "base":
                removing = local
            elif re.fullmatch(r"-[1-9]\d*", target):
                if -int(target) > len(local):
                    raise MigrationError(f"Not enough applied migrations of '{app.name}' for '{target}'.", app.name)
                removing = set(graph.order(local)[int(target) :])
            elif (app.name, target) in local:
                key = (app.name, target)
                removing = (graph.descendants_in_app([key]) & local) - {key}
            else:
                raise MigrationError(f"Target '{target}' is not an applied revision of '{app.name}'.", app.name)
            steps = _rollback_steps(graph, applied, removing, app.name, cascade)
            self._run_steps(engine, graph, steps, upgrade=False)
            return steps

    def uninstall(self, app_import_path: str, *, cascade: bool = False) -> list[MigrationKey]:
        """Revert the module to base; merely disabling it requires no migration."""
        return self.downgrade(app_import_path, "base", cascade=cascade)

    def adopt(self) -> int:
        """Import legacy version positions as protected baselines, never running user scripts."""
        with self._migration_lock(), self._engine() as engine:
            return self._ledger.adopt(engine, MigrationGraph.load(self._registry, legacy=True))

    def init_ledger(self, *, adopted_ledger: str) -> None:
        """Create an empty ledger for a genuinely new registry beside an adopted one.

        The caller must verify that no migration or data changes of these modules have
        previously run. Current model ownership cannot prove that historical fact.
        """
        from ..base import BedrockModel

        apps = {app.name for app in self._registry.all()}
        if not apps:
            raise MigrationError("Load the independent registry before initializing its ledger.")
        with self._migration_lock(), self._engine() as engine, engine.begin() as conn:
            MigrationGraph.load(self._registry)
            owned = [
                BedrockModel.metadata.tables[key]
                for key, owner in self._resolve_table_owners().items()
                if owner in apps
            ]
            self._ledger.initialize_empty(conn, adopted_ledger, apps, owned)

    def prepare_squash(
        self, app: str, first: str, through: str, *, out: Path, script: Path | None = None, rename: str | None = None
    ) -> Path:
        """Write a reviewable replacement candidate for ``first..through`` into *out*; no database is opened."""
        return squash_writer.prepare_candidate(
            self._registry, app, first, through, out=out, script=script, rename=rename
        )

    def _backend(self) -> str:
        return make_url(self._database_url).get_backend_name()

    def _squash_report(
        self, spec: squash.SquashSpec, states: dict[MigrationKey, str], *, applied: bool
    ) -> SquashReport:
        url = make_url(self._database_url)
        # Routing overrides change which server is reached, so keep them; drop credentials and the rest.
        route = {k: v for k, v in url.query.items() if k in {"host", "hostaddr", "port", "unix_socket", "service"}}
        replace = states[spec.end] == "replace"
        return SquashReport(
            target=URL.create(
                url.drivername, host=url.host, port=url.port, database=url.database, query=route
            ).render_as_string(),
            version_table=self.version_table,
            key=spec.end,
            state=states[spec.end],
            applied=applied,
            update=(spec.old[-1][0], spec.end) if replace else None,
            delete=tuple(key for key, _ in spec.old[:-1]) if replace else (),
            states=dict(states),
            remaining=tuple(sorted(k for k, s in states.items() if s == "replace" and k != spec.end)),
        )

    def _project_squashes(
        self,
        graph: MigrationGraph,
        specs: dict[MigrationKey, squash.SquashSpec],
        applied: dict[MigrationKey, ledger.AppliedMigration],
    ) -> tuple[dict[MigrationKey, str], dict[MigrationKey, ledger.AppliedMigration]]:
        """Project every pending replacement, then validate loaded modules against the projected ledger."""
        states, projected = ledger.project_squashes(applied, specs)
        for app in self._registry.all():
            self._validate_app(app.name, graph, projected)
        return states, projected

    def apply_squash(self, app: str, revision: str, *, apply: bool = False) -> SquashReport:
        """Preview, or with ``apply=True`` perform, the ledger switch for an installed replacement.

        No migration script runs and no business table is touched. Preview takes no lock,
        writes nothing and never creates a missing SQLite file. The write runs in one explicit
        transaction on one physical connection: PostgreSQL holds ``pg_advisory_xact_lock`` inside
        it (released by commit or rollback, safe behind poolers); MySQL holds ``GET_LOCK`` on it
        until after commit or rollback. A driver in autocommit mode is rejected before any lock
        or write. A lost connection fails the call, so rerun it to classify the committed ledger.

        Raises:
            MigrationError: *revision* is not an installed replacement end, the dialect or ledger
                storage is not eligible, the connection fails or uses autocommit, the ledger
                state is blocked, the lock times out, or the write fails (nothing is committed).
        """
        self._get_app(app)
        self._reject_database_selector()  # before any other check or connection
        graph = MigrationGraph.load(self._registry)
        specs = squash.installed_specs(graph)
        spec = specs.get((app, revision))
        if spec is None:
            raise MigrationError(f"'{app}:{revision}' is not the end of an installed squash description.", app)
        backend = self._backend()
        if backend not in {"sqlite", "postgresql", "mysql", "mariadb"}:
            raise MigrationError(
                f"Squash is not supported for database dialect '{backend}'; use SQLite, PostgreSQL or MySQL."
            )
        if not apply:
            with self._ledger_reader() as conn:
                states, _ = self._project_squashes(
                    graph, specs, {} if conn is None else self._ledger.read_applied(conn)
                )
                if conn is not None:
                    self._check_squash_storage(conn, backend, states[spec.end])
            return self._squash_report(spec, states, applied=False)
        try:
            with self._squash_transaction(create=False) as conn:
                return self._squash_locked(conn, graph, specs, spec, backend)
        except _MissingDatabase:
            states, _ = self._project_squashes(graph, specs, {})
            return self._squash_report(spec, states, applied=False)

    @contextmanager
    def _squash_transaction(self, *, create: bool = True) -> Iterator[Connection]:
        """Hold the cutover lock on the ledger connection until the caller commits or rolls back.

        With ``create=False`` a missing SQLite file raises :class:`_MissingDatabase` instead of being created.
        """
        self._reject_database_selector()
        backend = self._backend()
        sqlite = backend == "sqlite"
        # pysqlite autocommit mode leaves transaction control to the explicit BEGIN IMMEDIATE below.
        args = {"autocommit": True, "timeout": self._lock_timeout} if sqlite else {}
        target = None if create else self._sqlite_target("rw")
        try:
            if target:
                engine = create_engine(
                    "sqlite://", poolclass=NullPool, creator=lambda: sqlite3.connect(target[0], uri=True, **args)
                )
            else:
                engine = create_engine(self._database_url, connect_args=args)
        except SQLAlchemyError as exc:
            raise MigrationError(f"Cannot open the migration database: {exc}") from exc
        try:
            try:
                conn = engine.connect()
            except SQLAlchemyError as exc:
                if target and self._gone(target):
                    raise _MissingDatabase from exc
                raise MigrationError(f"Cannot connect to the migration database: {exc}") from exc
            with conn:
                locked = False
                try:
                    if sqlite:
                        conn.exec_driver_sql("BEGIN IMMEDIATE")
                    else:
                        if self._autocommit_hazard(conn):
                            raise MigrationError(
                                "The database connection is in autocommit mode, so the squash cannot be "
                                "atomic or locked; remove the driver autocommit setting and retry."
                            )
                        conn.rollback()
                        if backend == "postgresql":
                            conn.begin()
                            self._acquire_postgresql_xact_lock(conn)
                        else:
                            self._acquire_dialect_lock(conn, backend)
                            locked = True
                            conn.begin()
                    yield conn
                except BaseException as exc:
                    self._squash_end(conn, backend, commit=False)
                    if isinstance(exc, SQLAlchemyError):
                        wait = sqlite and isinstance(exc, OperationalError) and "locked" in str(exc)
                        raise MigrationError(
                            (f"Timed out after {self._lock_timeout:g}s waiting for the SQLite write lock: {exc}")
                            if wait
                            else f"Squash database operation failed: {exc}. Reconnect and preview before retrying."
                        ) from exc
                    raise
                finally:
                    if locked:
                        try:
                            self._release_dialect_lock(conn, backend)
                        except Exception:
                            log.debug("Failed to release the migration lock", exc_info=True)
        finally:
            engine.dispose()

    def _reject_database_selector(self) -> None:
        """Refuse URLs whose query can select a database other than the one the URL path names.

        Drivers let ``dbname``/``database``/``db`` override the path, so reports and the
        redacted target would name a different database than the one being changed.
        """
        url = make_url(self._database_url)
        clash = sorted(k for k in _DATABASE_SELECTORS & url.query.keys() if url.query[k] != url.database)
        if clash:
            raise MigrationError(
                f"Database URL query parameter(s) {', '.join(clash)} may select a different database than "
                "the URL path; put the database name in the path only."
            )

    def _sqlite_target(self, mode: str) -> tuple[str, str | None] | None:
        """URI opening the file-backed SQLite database with ``mode=ro|rw`` (never creating it), plus its path.

        None for other dialects, in-memory databases and ``mode=memory`` URIs, which have no file to create.
        The path is None when the URI names none.
        """
        url = make_url(self._database_url)
        database = url.database
        if url.get_backend_name() != "sqlite" or not database or database == ":memory:":
            return None
        if str(url.query.get("uri", "")).lower() in {"true", "1", "yes", "on"}:
            base, _, query = database.partition("?")
            # SQLAlchemy moves URI parameters written after the URL path into url.query.
            extra = {k: v for k, v in url.query.items() if k not in _PYSQLITE_CONNECT_ARGS and isinstance(v, str)}
            pairs = [p for p in query.split("&") if p] + [f"{k}={quote(v)}" for k, v in extra.items()]
            if not base.startswith("file:") or "mode=memory" in pairs:
                return None
            pairs = [p for p in pairs if not p.startswith("mode=")]
            return f"{base}?{'&'.join([*pairs, f'mode={mode}'])}", unquote(urlsplit(base).path) or None
        path = os.path.abspath(database)
        return f"file:{quote(path)}?mode={mode}", path

    @staticmethod
    def _gone(target: tuple[str, str | None]) -> bool:
        """True when a failed noncreating open was caused by a missing file (never created, so no race)."""
        return target[1] is not None and not os.path.exists(target[1])

    @contextmanager
    def _ledger_reader(self) -> Iterator[Connection | None]:
        """Yield a read-only ledger connection, or None when the SQLite file does not exist; never creates it."""
        self._reject_database_selector()
        target = self._sqlite_target("ro")
        if target is None:
            with self._engine() as engine, engine.connect() as conn:
                yield conn
            return
        engine = create_engine("sqlite://", poolclass=NullPool, creator=lambda: sqlite3.connect(target[0], uri=True))
        try:
            conn = None
            try:
                conn = engine.connect()
            except SQLAlchemyError as exc:
                if not self._gone(target):
                    raise MigrationError(f"Cannot connect to the migration database: {exc}") from exc
            if conn is None:
                yield None
                return
            with conn:
                try:
                    yield conn
                except SQLAlchemyError as exc:
                    raise MigrationError(f"Migration database operation failed: {exc}") from exc
        finally:
            engine.dispose()

    @staticmethod
    def _autocommit_hazard(conn: Connection) -> bool:
        """True when the driver or isolation level commits every statement on its own."""
        if conn.get_isolation_level() == "AUTOCOMMIT":
            return True
        raw = conn.connection.dbapi_connection
        getter = getattr(raw, "get_autocommit", None)  # pymysql, mysqlclient
        return (getter() if callable(getter) else getattr(raw, "autocommit", False)) is True

    def _acquire_postgresql_xact_lock(self, conn: Connection) -> None:
        """Poll ``pg_try_advisory_xact_lock`` inside the caller's transaction; commit or rollback releases it."""
        deadline = time.monotonic() + self._lock_timeout
        while not conn.execute(
            text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": _POSTGRES_MIGRATION_LOCK_KEY}
        ).scalar():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MigrationError(
                    f"Timed out after {self._lock_timeout:g}s waiting for the PostgreSQL migration lock."
                )
            time.sleep(min(0.05, remaining))

    def _check_squash_storage(self, conn: Connection, backend: str, state: str) -> None:
        if (
            state == "replace"
            and backend in {"mysql", "mariadb"}
            and str(self._ledger_storage_engine(conn)).lower() != "innodb"
        ):
            raise MigrationError(f"Ledger '{self.version_table}' must use InnoDB for a transactional squash.")

    def _squash_locked(
        self,
        conn: Connection,
        graph: MigrationGraph,
        specs: dict[MigrationKey, squash.SquashSpec],
        spec: squash.SquashSpec,
        backend: str,
    ) -> SquashReport:
        states, _ = self._project_squashes(graph, specs, self._ledger.read_applied(conn))
        if states[spec.end] != "replace":
            self._squash_end(conn, backend, commit=False)
            return self._squash_report(spec, states, applied=False)
        self._check_squash_storage(conn, backend, "replace")
        self._ledger.replace_squash(conn, spec)  # the ledger enforces the exact-row invariants
        self._squash_end(conn, backend, commit=True)
        return self._squash_report(spec, states, applied=True)

    def _ledger_storage_engine(self, conn: Connection) -> object:
        return conn.execute(
            text("SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"),
            {"t": self.version_table},
        ).scalar()

    @staticmethod
    def _squash_end(conn: Connection, backend: str, *, commit: bool) -> None:
        """Finish the squash transaction on its own connection; a failed rollback is not an error."""
        if backend == "sqlite":
            if commit:
                conn.exec_driver_sql("COMMIT")
            else:
                with suppress(SQLAlchemyError):
                    conn.exec_driver_sql("ROLLBACK")
        elif commit:
            conn.commit()
        else:
            with suppress(SQLAlchemyError):
                conn.rollback()

    def heads(self, app_import_path: str) -> tuple[str, ...]:
        """Return local names of all filesystem heads."""
        self._get_app(app_import_path)
        return tuple(rev for _, rev in MigrationGraph.load(self._registry).heads(app_import_path))

    @staticmethod
    def _current_heads(graph: MigrationGraph, local: set[MigrationKey]) -> tuple[str, ...]:
        parents = {parent for key in local for parent in graph[key].down_revisions}
        return tuple(sorted(rev for _, rev in local - parents))

    def current(self, app_import_path: str) -> tuple[str, ...]:
        """Return local names of applied heads without writing a version table."""
        self._get_app(app_import_path)
        graph = MigrationGraph.load(self._registry)
        with self._ledger_reader() as conn:
            if conn is None:
                return ()
            applied = self._ledger.read_applied(conn)
        for app in self._registry.all():
            self._validate_app(app.name, graph, applied)
        local = graph.revisions(app_import_path) & applied.keys()
        return self._current_heads(graph, local)

    def check(self, app_import_path: str) -> list[BranchRevisionStatus]:
        """Report status of the target and loaded modules with scripts; never initialize state."""
        self._get_app(app_import_path)
        graph = MigrationGraph.load(self._registry)
        names = [
            app_import_path,
            *(app.name for app in self._registry.all() if app.name != app_import_path and graph.revisions(app.name)),
        ]
        self._reject_database_selector()  # before the try: a bad URL is an error, not "unknown"
        try:
            with self._ledger_reader() as conn:
                applied = {} if conn is None else self._ledger.read_applied(conn)
        except MigrationError:
            return [BranchRevisionStatus(n, (), tuple(r for _, r in graph.heads(n)), "unknown") for n in names]
        recorded_apps = {key[0] for key in applied}
        names.extend(app.name for app in self._registry.all() if app.name in recorded_apps and app.name not in names)
        result = []
        for name in names:
            heads = tuple(r for _, r in graph.heads(name))
            current: tuple[str, ...] = ()
            state = "unknown"
            try:
                self._validate_app(name, graph, applied)
                current = self._current_heads(graph, graph.revisions(name) & applied.keys())
                if current:
                    state = "at-head" if current == heads else "behind"
            except MigrationError:
                log.debug("Inconsistent applied migrations for '%s'", name, exc_info=True)
            result.append(BranchRevisionStatus(name, current, heads, state))
        return result

    def get_history(self, app_import_path: str) -> list[RevisionHistoryEntry]:
        """Return scripts in reverse dependency order, newest first."""
        self._get_app(app_import_path)
        graph = MigrationGraph.load(self._registry)
        heads = set(graph.heads(app_import_path))
        return [
            RevisionHistoryEntry(
                revision=r[1],
                down_revision=", ".join(p[1] for p in graph[r].down_revisions) or "base",
                depends_on=", ".join(f"{a}:{v}" for a, v in graph[r].depends_on),
                message=graph[r].message,
                is_head=r in heads,
            )
            for r in reversed(graph.order(graph.revisions(app_import_path)))
        ]

    def _resolve_table_owners(self) -> dict[str, str]:
        """Resolve each shared metadata table to one loaded app.

        Model definition packages determine ownership, not the modules importing
        them. Unmapped tables must declare ``info['bedrock_module']`` explicitly.

        Returns:
            A per-operation snapshot of table keys and owning app import paths.

        Raises:
            MigrationError: If a table's owner is missing, invalid, or ambiguous.
        """
        from ..base import BedrockModel

        apps = {app.name: app for app in self._registry.all()}

        # SQLAlchemy metadata is process-wide. Recognize already-imported, manifest-backed
        # modules outside this registry without importing or registering them here.
        def package_owner(module: str) -> str | None:
            while module:
                if module in apps:
                    return module
                pkg = sys.modules.get(module)
                if pkg is not None and getattr(pkg, "__path__", None) and (file := getattr(pkg, "__file__", None)):
                    if Path(file).with_name("manifest.yaml").is_file() and module + ".models" in sys.modules:
                        return module
                module = module.rpartition(".")[0]
            return None

        candidates: dict[str, set[str]] = {}
        definitions: dict[str, set[str]] = {}
        unresolved: set[str] = set()
        for mapper in BedrockModel.registry.mappers:
            table = mapper.local_table
            if not isinstance(table, Table) or BedrockModel.metadata.tables.get(table.key) is not table:
                continue
            model = mapper.class_
            definitions.setdefault(table.key, set()).add(f"{model.__module__}.{model.__qualname__}")
            owner = package_owner(model.__module__)
            if owner is not None:
                candidates.setdefault(table.key, set()).add(owner)
            else:
                unresolved.add(table.key)

        owners: dict[str, str] = {}
        for key, table in sorted(BedrockModel.metadata.tables.items()):
            inferred = candidates.get(key, set())
            source = f"Table '{key}' (models: {sorted(definitions.get(key, set()))!r})"
            if len(inferred) > 1:
                raise MigrationError(
                    f"{source} is mapped by multiple apps {sorted(inferred)!r}; "
                    "keep all mappings of one table in its owning app."
                )
            owner = next(iter(inferred), None)
            if "bedrock_module" in table.info:
                declared = table.info["bedrock_module"]
                if not isinstance(declared, str) or not declared or package_owner(declared) != declared:
                    raise MigrationError(f"{source} must declare info['bedrock_module'] as a loaded app import path.")
                if owner is not None and declared != owner:
                    raise MigrationError(
                        f"{source} declares owner '{declared}' but its models belong to '{owner}'; "
                        "make the declaration agree with the model's defining app."
                    )
                owner = declared
            elif key in unresolved:
                raise MigrationError(
                    f"{source} includes models without a loaded owning app; load each defining app "
                    "or declare info['bedrock_module'] explicitly for this table."
                )
            if owner is None:
                raise MigrationError(
                    f"{source} has no owning app; load its defining app or declare "
                    "info['bedrock_module'] on an unmapped Table."
                )
            if owner in apps and apps[owner].models_module is None:
                raise MigrationError(f"{source} belongs to '{owner}', which must provide a models module.", owner)
            owners[key] = owner
        return owners

    @contextmanager
    def _migration_lock(self) -> Iterator[None]:
        """Hold the dialect migration lock for one upgrade or downgrade.

        PostgreSQL takes ``pg_try_advisory_lock`` until :attr:`_lock_timeout`
        elapses. MySQL takes ``GET_LOCK`` with that timeout rounded up to whole
        seconds. SQLite yields immediately and does not serialize migrations;
        run SQLite upgrades from a single process.

        The lock is held on a dedicated connection that is committed after
        acquisition, so the session-level lock stays while Alembic uses its own
        connection. Failure to acquire the lock raises :class:`MigrationError`.

        Yields:
            Control to the locked operation.

        Raises:
            MigrationError: If the lock is not acquired before the timeout, or
                the dialect has no lock implementation.
        """

        dialect = make_url(self._database_url).get_backend_name()
        if dialect == "sqlite":
            yield
            return

        engine = create_engine(self._database_url)
        try:
            with engine.connect() as connection:
                self._acquire_dialect_lock(connection, dialect)
                try:
                    yield
                finally:
                    try:
                        self._release_dialect_lock(connection, dialect)
                    except Exception:
                        log.debug("Failed to release the migration lock", exc_info=True)
        finally:
            engine.dispose()

    def _acquire_dialect_lock(self, connection: _LockConnection, dialect: str) -> None:
        """Acquire the migration lock for *dialect* on *connection*.

        Args:
            connection: Open SQLAlchemy connection. It must support ``execute``
                and ``commit``.
            dialect: Backend name from the configured URL.

        Raises:
            MigrationError: If the dialect is unsupported or the wait times out.
        """

        if dialect == "postgresql":
            self._acquire_postgresql_lock(connection)
            return
        if dialect in {"mysql", "mariadb"}:
            self._acquire_mysql_lock(connection)
            return
        raise MigrationError(
            f"Cannot lock migrations for database dialect '{dialect}'. "
            "Supported lock dialects are PostgreSQL and MySQL; SQLite runs unlocked."
        )

    def _release_dialect_lock(self, connection: _LockConnection, dialect: str) -> None:
        """Release the migration lock acquired for *dialect*."""

        if dialect == "postgresql":
            connection.execute(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": _POSTGRES_MIGRATION_LOCK_KEY},
            )
            connection.commit()
            return
        if dialect in {"mysql", "mariadb"}:
            connection.execute(
                text("SELECT RELEASE_LOCK(:name)"),
                {"name": _MYSQL_MIGRATION_LOCK_NAME},
            )
            connection.commit()

    def _acquire_postgresql_lock(self, connection: _LockConnection) -> None:
        """Poll ``pg_try_advisory_lock`` until the timeout.

        ``lock_timeout`` does not apply to PostgreSQL advisory locks, so the
        wait is implemented here.
        """

        deadline = time.monotonic() + self._lock_timeout
        while True:
            acquired = connection.execute(
                text("SELECT pg_try_advisory_lock(:key)"),
                {"key": _POSTGRES_MIGRATION_LOCK_KEY},
            ).scalar()
            connection.commit()
            if acquired:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MigrationError(
                    f"Timed out after {self._lock_timeout:g}s waiting for the PostgreSQL migration lock."
                )
            time.sleep(min(0.05, remaining))

    def _acquire_mysql_lock(self, connection: _LockConnection) -> None:
        """Take ``GET_LOCK`` and raise when MySQL reports a timeout."""

        timeout_seconds = 0 if self._lock_timeout <= 0 else max(1, math.ceil(self._lock_timeout))
        result = connection.execute(
            text("SELECT GET_LOCK(:name, :timeout)"),
            {"name": _MYSQL_MIGRATION_LOCK_NAME, "timeout": timeout_seconds},
        ).scalar()
        connection.commit()
        if result != 1:
            raise MigrationError(f"Timed out after {self._lock_timeout:g}s waiting for the MySQL migration lock.")


__all__ = ["BranchRevisionStatus", "MigrationError", "MigrationsManager", "RevisionHistoryEntry", "SquashReport"]
