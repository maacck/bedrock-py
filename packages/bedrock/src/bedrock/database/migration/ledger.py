"""The migration ledger (default table ``bedrock_migrations``): which migrations are recorded as applied.

Every method expects the caller to hold the migration lock. Reads never return an
empty result on failure: an unreadable ledger is a ``MigrationError``.
"""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha1

import sqlalchemy as sa
from sqlalchemy import Column, Connection, DateTime, Engine, MetaData, String, Table, Text, delete, insert, select
from sqlalchemy.exc import SQLAlchemyError

from .graph import Migration, MigrationError, MigrationGraph, MigrationKey

VERSION_TABLE_NAME = "bedrock_migrations"
STAGING_TABLE_NAME = "bedrock_migrations_adopt"
LEGACY_VERSION_TABLE = "alembic_version"

_ORIGINS = frozenset({"baseline", "executed"})
_STAGING_SUFFIX = "_adopt"
_DEFAULT_STAGE_MARKER = "bedrock_adopt_v1_origin"
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")
# The staging name adds len("_adopt") == 6 characters; MySQL/MariaDB truncate-or-reject beyond 64.
_MAX_VERSION_TABLE_LENGTH = 57

_Baseline = list[tuple[MigrationKey, tuple[MigrationKey, ...]]]


@dataclass(frozen=True)
class AppliedMigration:
    """One row of a migration ledger."""

    app: str
    revision: str
    parents: tuple[MigrationKey, ...]  # every direct parent, same- and cross-module; unique and sorted
    origin: str  # baseline | executed; unknown values are errors


def _encode(deps: tuple[MigrationKey, ...]) -> str:
    return json.dumps([f"{a}:{r}" for a, r in deps])


def _decode(raw: str) -> tuple[MigrationKey, ...]:
    keys = []
    values = json.loads(raw)
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        raise ValueError("expected a JSON array of qualified migration names")
    for item in values:
        app, sep, revision = item.partition(":")
        if not sep or not app or not revision or ":" in revision:
            raise ValueError(f"malformed dependency {item!r}")
        keys.append((app, revision))
    return tuple(sorted(set(keys)))


def _legacy_error() -> MigrationError:
    return MigrationError(
        f"This database records migrations in '{LEGACY_VERSION_TABLE}'. Load every module and run "
        "'bedrock db adopt <app> ...' once before any other migration command."
    )


def _legacy_heads(conn: Connection) -> list[str]:
    try:
        columns = {c["name"] for c in sa.inspect(conn).get_columns(LEGACY_VERSION_TABLE)}
        if "version_num" not in columns:
            raise MigrationError(f"'{LEGACY_VERSION_TABLE}' has no version_num column; refusing to adopt.")
        quoted = conn.dialect.identifier_preparer.quote(LEGACY_VERSION_TABLE)
        return [r[0] for r in conn.exec_driver_sql(f"SELECT version_num FROM {quoted}")]
    except SQLAlchemyError as exc:
        raise MigrationError(f"Cannot read '{LEGACY_VERSION_TABLE}': {exc}") from exc


def _baseline(graph: MigrationGraph, heads: list[str]) -> _Baseline:
    """Resolve each legacy head to exactly one module, then expand its proven ancestry."""
    owners_of: dict[str, list[str]] = {}
    for app, revision in graph.migrations:
        owners_of.setdefault(revision, []).append(app)
    start: set[MigrationKey] = set()
    for head in heads:
        owners = sorted(owners_of.get(head, ()))
        if not owners:
            raise MigrationError(
                f"Legacy head '{head}' matches no migration of any loaded module; nothing was written."
            )
        if len(owners) > 1:
            raise MigrationError(
                f"Legacy head '{head}' is ambiguous: it exists in {', '.join(owners)}; nothing was written."
            )
        start.add((owners[0], head))
    return [(key, graph[key].parents) for key in graph.order(graph.ancestors(start))]


class MigrationLedger:
    """A ledger table and its adopt staging table, both bound to one ``version_table``.

    Legacy ``alembic_version`` state always fails closed, whatever the target table is;
    history is only ever moved by an explicit ``adopt``.
    """

    def __init__(self, version_table: str = VERSION_TABLE_NAME) -> None:
        if (
            not isinstance(version_table, str)
            or not _IDENTIFIER.fullmatch(version_table)
            or len(version_table) > _MAX_VERSION_TABLE_LENGTH
            or version_table.endswith(_STAGING_SUFFIX)
            or version_table == LEGACY_VERSION_TABLE
        ):
            raise MigrationError(
                f"Invalid migration version table {version_table!r}: use a lowercase identifier "
                f"([a-z_][a-z0-9_]*) of at most {_MAX_VERSION_TABLE_LENGTH} characters that does not end "
                f"in '{_STAGING_SUFFIX}' and is not '{LEGACY_VERSION_TABLE}'."
            )
        self.name = version_table
        self.staging_name = version_table + _STAGING_SUFFIX
        # MySQL scopes CHECK names to the schema, so each ledger needs its own marker.
        self._stage_marker = (
            _DEFAULT_STAGE_MARKER
            if version_table == VERSION_TABLE_NAME
            else f"{_DEFAULT_STAGE_MARKER}_{sha1(version_table.encode()).hexdigest()[:12]}"
        )
        # Kept outside BedrockModel.metadata so table-ownership resolution never sees it.
        self._table = Table(
            self.name,
            MetaData(),
            Column("app", String(255), primary_key=True),
            Column("revision", String(64), primary_key=True),
            Column("parents", Text, nullable=False),  # JSON list of app:revision strings
            Column("origin", String(8), nullable=False),  # baseline | executed
            Column("applied_at", DateTime(timezone=True), nullable=False),
        )
        self._staging = self._table.to_metadata(MetaData(), name=self.staging_name)
        # A marker distinguishes our scratch table from unrelated tables with matching columns.
        self._staging.append_constraint(
            sa.CheckConstraint("origin IN ('executed', 'baseline')", name=self._stage_marker)
        )

    def _schema_problem(self, conn: Connection, name: str) -> str | None:
        inspector = sa.inspect(conn)
        columns = {c["name"]: c for c in inspector.get_columns(name)}
        pk = inspector.get_pk_constraint(name)["constrained_columns"]

        def matches(col: Column) -> bool:
            actual = columns[col.name]
            kind = actual["type"]
            return (
                not actual["nullable"]
                and isinstance(kind, type(col.type))
                and getattr(kind, "length", None) == getattr(col.type, "length", None)
            )

        table = self._table
        if (
            set(columns) != set(table.c.keys())
            or list(pk) != ["app", "revision"]
            or not all(matches(c) for c in table.c)
        ):
            return (
                f"Table '{name}' exists but is not a Bedrock migration ledger "
                f"(columns {sorted(columns)}, primary key {sorted(pk)})."
            )
        return None

    def read_applied(self, conn: Connection) -> dict[MigrationKey, AppliedMigration]:
        """Return every recorded migration; ``{}`` only when no ledger exists at all.

        Raises:
            MigrationError: only the legacy Alembic table exists, or the ledger is unreadable.
        """
        table = self._table
        try:
            inspector = sa.inspect(conn)
            if not inspector.has_table(self.name):
                if inspector.has_table(LEGACY_VERSION_TABLE):
                    raise _legacy_error()
                return {}
            if problem := self._schema_problem(conn, self.name):
                raise MigrationError(problem)
            result: dict[MigrationKey, AppliedMigration] = {}
            for row in conn.execute(select(table.c.app, table.c.revision, table.c.parents, table.c.origin)):
                if row.origin not in _ORIGINS:
                    raise MigrationError(f"Ledger row {row.app}:{row.revision} has unknown origin {row.origin!r}.")
                result[(row.app, row.revision)] = AppliedMigration(
                    row.app, row.revision, _decode(row.parents), row.origin
                )
            return result
        except MigrationError:
            raise
        except (SQLAlchemyError, ValueError, TypeError) as exc:
            raise MigrationError(f"Cannot read '{self.name}': {exc}") from exc

    def ensure_table(self, conn: Connection) -> None:
        """Create the ledger when absent; refuse to start one next to un-adopted legacy state."""
        inspector = sa.inspect(conn)
        if not inspector.has_table(self.name):
            if inspector.has_table(LEGACY_VERSION_TABLE):
                raise _legacy_error()
            self._table.create(conn)
        elif problem := self._schema_problem(conn, self.name):
            raise MigrationError(problem)

    def initialize_empty(self, conn: Connection, adopted_ledger: str, apps: set[str], owned: list[Table]) -> None:
        """Create only the target table, after validating the prior adoption under the migration lock."""
        source = MigrationLedger(adopted_ledger)
        inspector = sa.inspect(conn)
        if self.name == source.name or inspector.has_table(self.name):
            raise MigrationError(f"Ledger '{self.name}' is not a new, independent ledger.")
        for name in (self.staging_name, source.staging_name):
            if inspector.has_table(name):
                raise MigrationError(f"Staging table '{name}' exists; resolve the interrupted adoption first.")
        if not inspector.has_table(LEGACY_VERSION_TABLE) or not inspector.has_table(source.name):
            raise MigrationError(f"Expected a legacy version table and a previously adopted ledger '{source.name}'.")
        rows = source.read_applied(conn)
        markers = inspector.get_check_constraints(source.name)
        baselines = {row.revision for row in rows.values() if row.origin == "baseline"}
        if not any(c["name"] == source._stage_marker for c in markers) or not set(_legacy_heads(conn)) <= baselines:
            raise MigrationError(f"Ledger '{source.name}' does not prove adoption of the legacy heads.")
        overlap = apps.intersection(row.app for row in rows.values())
        if overlap:
            raise MigrationError(f"Modules already recorded in '{source.name}': {', '.join(sorted(overlap))}.")
        for table in owned:
            if table.name.lower() in {self.name, self.staging_name} and table.schema in {
                None,
                conn.dialect.default_schema_name,
            }:
                raise MigrationError(f"Model table '{table.fullname}' conflicts with the new ledger name.")
            if inspector.has_table(table.name, schema=table.schema):
                raise MigrationError(
                    f"Module table '{table.fullname}' already exists; refusing to start an empty ledger."
                )
        self._table.create(conn)

    def record(self, conn: Connection, migration: Migration) -> None:
        conn.execute(
            insert(self._table).values(
                app=migration.app,
                revision=migration.revision,
                parents=_encode(migration.parents),
                origin="executed",
                applied_at=datetime.now(UTC),
            )
        )

    def forget(self, conn: Connection, key: MigrationKey) -> None:
        conn.execute(delete(self._table).where(self._table.c.app == key[0], self._table.c.revision == key[1]))

    # --- adopt (ADR-9) -------------------------------------------------------------

    def _check_own_staging(self, conn: Connection) -> None:
        """A leftover staging table is ours only if it has the ledger shape and baseline rows only."""
        problem = self._schema_problem(conn, self.staging_name)
        origins = set() if problem else {r[0] for r in conn.execute(select(self._staging.c.origin).distinct())}
        checks = sa.inspect(conn).get_check_constraints(self.staging_name)
        if problem or not origins <= {"baseline"} or not any(c["name"] == self._stage_marker for c in checks):
            raise MigrationError(
                f"Table '{self.staging_name}' exists but was not left by an interrupted adopt; "
                "inspect and remove it manually. Nothing was changed."
            )

    def _create_staging(self, engine: Engine, replace: bool) -> None:
        with engine.begin() as conn:
            if replace:
                self._staging.drop(conn)
            self._staging.create(conn)

    def _fill_staging(self, engine: Engine, rows: _Baseline) -> None:
        staging = self._staging
        now = datetime.now(UTC)
        with engine.begin() as conn:
            if rows:
                conn.execute(
                    insert(staging),
                    [
                        {
                            "app": k[0],
                            "revision": k[1],
                            "parents": _encode(d),
                            "origin": "baseline",
                            "applied_at": now,
                        }
                        for k, d in rows
                    ],
                )
            stored = [
                ((r.app, r.revision), _decode(r.parents), r.origin)
                for r in conn.execute(select(staging.c.app, staging.c.revision, staging.c.parents, staging.c.origin))
            ]
            if sorted(stored) != sorted((k, d, "baseline") for k, d in rows):
                raise MigrationError("Staged baseline does not match the computed baseline; nothing was published.")

    def _publish(self, engine: Engine) -> None:
        with engine.begin() as conn:
            quote = conn.dialect.identifier_preparer.quote
            conn.exec_driver_sql(f"ALTER TABLE {quote(self.staging_name)} RENAME TO {quote(self.name)}")

    def adopt(self, engine: Engine, graph: MigrationGraph) -> int:
        """Import the legacy Alembic position as a protected baseline; return the row count.

        The caller holds the migration lock. The baseline is built in a staging table and
        renamed to the final name, so the final table is either absent or complete. The final
        table is never dropped. On MySQL/MariaDB, DDL commits implicitly; a failure leaves at
        most the staging table, which a later explicit adopt may rebuild.

        Raises:
            MigrationError: already adopted, no legacy state, unresolved/ambiguous head,
                conflicting tables, or a staging table of unknown origin.
        """
        with engine.connect() as conn:
            inspector = sa.inspect(conn)
            if inspector.has_table(self.name):
                if problem := self._schema_problem(conn, self.name):
                    raise MigrationError(problem)
                raise MigrationError(f"'{self.name}' already exists; the database is already initialized.")
            if not inspector.has_table(LEGACY_VERSION_TABLE):
                raise MigrationError(f"No '{LEGACY_VERSION_TABLE}' table found; there is nothing to adopt.")
            rows = _baseline(graph, _legacy_heads(conn))
            stale = inspector.has_table(self.staging_name)
            if stale:
                self._check_own_staging(conn)
        self._create_staging(engine, stale)
        self._fill_staging(engine, rows)
        self._publish(engine)
        with engine.connect() as conn:
            applied = self.read_applied(conn)
        if set(applied) != {k for k, _ in rows} or any(a.origin != "baseline" for a in applied.values()):
            raise MigrationError(f"Published '{self.name}' does not match the computed baseline; inspect it.")
        return len(rows)
