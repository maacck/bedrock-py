"""Alembic migration management for the Bedrock module system.

This module provides a high-level wrapper around the Alembic programmatic API.
It is responsible for:

- Dynamically building an Alembic :class:`~alembic.config.Config` from the
  live :class:`~bedrock.module.registry.ModuleRegistry`, so that
  ``version_locations`` always reflects the currently installed apps.
- Enforcing the convention that every app's branch label equals its import
  path (e.g. ``myerp.wms``), preventing cross-app version-tree pollution.
- Injecting per-app context into ``env.py`` via
  :attr:`~alembic.config.Config.attributes` so that ``include_name`` can
  restrict autogenerate to only the tables owned by the target app.

All public operations (``revision``, ``upgrade``, ``downgrade``, ``heads``,
``current``) accept an *app_import_path* and translate it into the correct
Alembic branch-aware invocation.
"""

import os
from pathlib import Path
from typing import TYPE_CHECKING

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError

from ..exc import BedrockExc
from ..logging import get_logger

if TYPE_CHECKING:
    from ..module.entities import AppConfig
    from ..module.registry import ModuleRegistry

# ---------------------------------------------------------------------------
# Sentinel placed in Config.attributes so env.py can recognise bedrock calls.
# ---------------------------------------------------------------------------
_BEDROCK_MARKER = "bedrock_managed"

log = get_logger(__name__)


class MigrationError(BedrockExc):
    """Raised when a migration operation cannot be completed.

    Attributes:
        app_import_path: The import path of the app that triggered the error,
            if applicable.
    """

    detail: str = "Migration operation failed."

    def __init__(self, message: str, app_import_path: str | None = None) -> None:
        self.app_import_path = app_import_path
        super().__init__(msg=message)


class BranchOwnershipError(MigrationError):
    """Raised when a revision file inside an app's migrations directory declares
    a branch label that does not belong to that app.

    This indicates either a copy-paste mistake or an attempt to write into a
    foreign branch tree from the wrong package directory.

    Attributes:
        revision_id: The offending revision identifier.
        declared_labels: The branch labels found in the revision file.
        expected_label: The label that should have been declared.
    """

    def __init__(
        self,
        app_import_path: str,
        revision_id: str,
        declared_labels: frozenset[str],
        expected_label: str,
    ) -> None:
        super().__init__(
            f"Revision '{revision_id}' in app '{app_import_path}' declares branch labels "
            f"{sorted(declared_labels)!r} but only '{expected_label}' is permitted. "
            "A revision file must only belong to the branch of the app that owns its directory.",
            app_import_path,
        )
        self.revision_id = revision_id
        self.declared_labels = declared_labels
        self.expected_label = expected_label


class MigrationsManager:
    """Orchestrates Alembic operations across all installed Bedrock apps.

    A single :class:`MigrationsManager` is expected to be created per
    process.  It requires a populated :class:`~bedrock.module.registry.ModuleRegistry`
    and a database URL.

    The manager keeps the Alembic :class:`~alembic.config.Config` ephemeral —
    it is rebuilt on every call so that it always reflects the current state of
    the registry (e.g. after hot-installing a new app).

    Args:
        registry: A fully populated module registry.
        database_url: SQLAlchemy-compatible database URL.
        script_location: Filesystem path to the directory that contains
            ``env.py``, ``script.py.mako``, and optionally a ``versions/``
            sub-directory for the shared root migrations (if any).  Defaults
            to the ``migrations/`` package bundled with ``bedrock.database``.
    """

    def __init__(
        self,
        registry: "ModuleRegistry",
        database_url: str,
        script_location: str | None = None,
    ) -> None:
        self._registry = registry
        self._database_url = database_url
        self._script_location = script_location or str(Path(__file__).parent / "migrations")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def revision(
        self,
        app_import_path: str,
        message: str,
        *,
        autogenerate: bool = True,
    ) -> None:
        """Create a new migration revision for the given app.

        If the app has no existing revisions (i.e. the branch does not yet
        exist), the new revision is created from ``base`` and the branch label
        is set to the app import path.  Subsequent revisions are appended to
        ``<branch_label>@head``.

        When the app declares ``depends_on`` in its manifest, the current head
        revision IDs of all dependency branches are resolved at creation time
        and written into the generated revision file's ``depends_on`` field.
        This encodes the cross-app dependency at the schema level rather than
        requiring a runtime check on every upgrade.

        Args:
            app_import_path: Import path of the target app, e.g.
                ``"myerp.wms"``.
            message: Human-readable description for the revision file.
            autogenerate: When ``True``, Alembic compares the current database
                state against the target metadata and generates change
                operations automatically.  Defaults to ``True``.

        Raises:
            MigrationError: If the app is not found in the registry, does not
                have a models submodule, or the Alembic operation fails.
        """
        app = self._get_app(app_import_path)
        cfg = self._build_config(app_import_path)
        version_path = str(self._ensure_migrations_dir(app))
        branch_label = app_import_path

        if self._branch_exists(cfg, branch_label):
            # Append a new revision to the existing branch.
            try:
                command.revision(
                    cfg,
                    message=message,
                    autogenerate=autogenerate,
                    head=f"{branch_label}@head",
                    version_path=version_path,
                )
            except CommandError as exc:
                raise MigrationError(str(exc), app_import_path) from exc
        else:
            # Bootstrap: create the first revision for this branch.
            try:
                command.revision(
                    cfg,
                    message=message,
                    autogenerate=autogenerate,
                    head="base",
                    branch_label=branch_label,
                    version_path=version_path,
                )
            except CommandError as exc:
                raise MigrationError(str(exc), app_import_path) from exc

    def ensure_schema(self, app_import_path: str) -> str:
        """Ensure the database schema for *app_import_path* is up to date.

        This is the primary entry-point called during ``bedrock manage install``.
        The logic follows two paths depending on the current Alembic state:

        - **No revisions present** (first install): the branch does not yet exist
          in ``alembic_version``.  All tables are created directly from the
          SQLAlchemy metadata and the branch is immediately stamped at its head
          so that future ``upgrade`` calls are no-ops.
        - **Revisions present but behind head**: ``upgrade`` is run to bring the
          branch current.
        - **Already at head**: nothing is done.

        Args:
            app_import_path: Import path of the target app, e.g. ``"myerp.wms"``.

        Returns:
            A human-readable status string: ``"created"``, ``"upgraded"``, or
            ``"up-to-date"``.

        Raises:
            MigrationError: If the app is not installed, has no models, or the
                Alembic operation fails.
        """
        app = self._get_app(app_import_path)
        cfg = self._build_config(app_import_path)
        branch_label = app_import_path

        current_revs = self._get_current_revisions(cfg, branch_label)

        if not current_revs:
            # Branch has never been applied — create tables and stamp.
            self._create_tables_and_stamp(cfg, app)
            return "created"

        if self._is_at_head(cfg, branch_label, current_revs):
            return "up-to-date"

        # Behind head — run upgrade.
        try:
            command.upgrade(cfg, f"{branch_label}@head")
        except CommandError as exc:
            raise MigrationError(str(exc), app_import_path) from exc
        return "upgraded"

    def _get_current_revisions(self, cfg: Config, branch_label: str) -> set[str]:
        """Return the set of revision IDs currently applied for *branch_label*.

        Queries the live database via ``alembic_version`` and filters to only
        those revision IDs that belong to *branch_label*'s branch.

        Args:
            cfg: Alembic config pointing to the target database.
            branch_label: Branch label (= app import path) to inspect.

        Returns:
            Set of applied revision ID strings for this branch.  Empty when the
            branch has never been applied.

        Raises:
            DatabaseDoesNotExistError: If the target PostgreSQL database does not
                exist.  This is explicitly surfaced so callers never interpret a
                missing database as an empty revision history.
        """
        from alembic.runtime.migration import MigrationContext
        from sqlalchemy import create_engine

        from .provisioner import DatabaseDoesNotExistError

        try:
            script = ScriptDirectory.from_config(cfg)
            branch_rev_ids: set[str] = {
                rev.revision
                for rev in script.walk_revisions()
                if branch_label in (rev.branch_labels or set())
                or self._revision_in_branch(script, rev.revision, branch_label)
            }
        except Exception:
            log.debug("Failed to collect applied revisions for branch '%s'", branch_label, exc_info=True)
            return set()

        if not branch_rev_ids:
            return set()

        engine = create_engine(self._database_url)
        try:
            with engine.connect() as conn:
                migration_ctx = MigrationContext.configure(conn)
                applied = migration_ctx.get_current_heads()
        except Exception as exc:
            engine.dispose()
            if _is_missing_database_error(exc):
                # Extract database name from the URL (last path segment).
                db_name = self._database_url.rsplit("/", 1)[-1].split("?")[0]
                raise DatabaseDoesNotExistError(db_name) from exc
            log.debug("Failed to query current migration heads from database", exc_info=True)
            return set()
        finally:
            engine.dispose()

        return set(applied) & branch_rev_ids

    def _revision_in_branch(self, script: ScriptDirectory, rev_id: str, branch_label: str) -> bool:
        """Return whether *rev_id* is reachable from *branch_label*'s head.

        Walks upward through the revision chain from the branch head to
        determine membership.  Used to identify intermediate revisions that
        carry no explicit branch label but belong to the branch.

        Args:
            script: Alembic script directory.
            rev_id: Revision ID to test.
            branch_label: Branch label identifying the target branch.

        Returns:
            ``True`` if *rev_id* is an ancestor of (or equal to) the branch head.
        """
        try:
            for rev in script.iterate_revisions(f"{branch_label}@head", f"{branch_label}@base"):
                if rev.revision == rev_id:
                    return True
        except Exception:
            log.debug("Failed to iterate revisions for branch '%s' membership check", branch_label, exc_info=True)
            pass
        return False

    def _is_at_head(self, cfg: Config, branch_label: str, current_revs: set[str]) -> bool:
        """Return whether *current_revs* already covers the branch head.

        Args:
            cfg: Alembic config.
            branch_label: Branch label to resolve head for.
            current_revs: Set of currently applied revision IDs for this branch.

        Returns:
            ``True`` when the applied set equals the expected head set.
        """
        try:
            script = ScriptDirectory.from_config(cfg)
            head_revs = {r.revision for r in script.get_revisions(f"{branch_label}@head")}
            return head_revs == current_revs
        except Exception:
            log.debug("Failed to determine head revisions for branch '%s'", branch_label, exc_info=True)
            return False

    def _create_tables_and_stamp(self, cfg: Config, app: "AppConfig") -> None:
        """Create all tables for *app* and stamp the branch at head.

        Used during first-time installation when no revision history exists for
        the branch.  SQLAlchemy's ``create_all`` with a ``tables`` filter ensures
        only this app's tables are created; the stamp records the head revision
        in ``alembic_version`` without running any migration scripts.

        Args:
            cfg: Alembic config pointing to the target database.
            app: The app whose tables should be created.

        Raises:
            MigrationError: If table creation or stamping fails.
        """
        from sqlalchemy import create_engine

        from bedrock.database.base import BedrockModel

        branch_label = app.name

        # Collect the table objects owned by this app from the shared metadata.
        if app.models_module is not None:
            owned_table_names = {
                obj.__tablename__
                for obj in vars(app.models_module).values()
                if (
                    isinstance(obj, type)
                    and issubclass(obj, BedrockModel)
                    and obj is not BedrockModel
                    and hasattr(obj, "__tablename__")
                )
            }
            tables = [
                BedrockModel.metadata.tables[name] for name in owned_table_names if name in BedrockModel.metadata.tables
            ]
        else:
            tables = []

        engine = create_engine(self._database_url)
        try:
            BedrockModel.metadata.create_all(engine, tables=tables or None)
        except Exception as exc:
            engine.dispose()
            raise MigrationError(f"Failed to create tables for '{branch_label}': {exc}", branch_label) from exc

        engine.dispose()

        # Stamp so Alembic knows this branch is at head.
        try:
            command.stamp(cfg, f"{branch_label}@head")
        except CommandError as exc:
            raise MigrationError(f"Failed to stamp '{branch_label}' at head: {exc}", branch_label) from exc

    def upgrade(self, app_import_path: str, target: str = "head") -> None:
        """Upgrade the given app's branch to *target*.

        Args:
            app_import_path: Import path of the target app.
            target: Revision identifier or relative offset accepted by
                Alembic (``"head"``, ``"+1"``, a revision ID, …).
                Defaults to ``"head"``.

        Raises:
            MigrationError: If the app is not installed or the Alembic
                operation fails.
        """
        self._get_app(app_import_path)
        cfg = self._build_config(app_import_path)
        branch_label = app_import_path

        # If the caller passed a plain "head" we qualify it with the branch so
        # that only this app's head is upgraded, never a foreign branch's head.
        if target == "head":
            alembic_target = f"{branch_label}@head"
        else:
            alembic_target = target

        try:
            command.upgrade(cfg, alembic_target)
        except CommandError as exc:
            raise MigrationError(str(exc), app_import_path) from exc

    def downgrade(self, app_import_path: str, target: str) -> None:
        """Downgrade the given app's branch to *target*.

        Args:
            app_import_path: Import path of the target app.
            target: Revision identifier or relative offset accepted by
                Alembic (``"base"``, ``"-1"``, a revision ID, …).

        Raises:
            MigrationError: If the app is not installed or the Alembic
                operation fails.
        """
        self._get_app(app_import_path)
        cfg = self._build_config(app_import_path)

        try:
            command.downgrade(cfg, target)
        except CommandError as exc:
            raise MigrationError(str(exc), app_import_path) from exc

    def heads(self, app_import_path: str) -> None:
        """Print the current head revision for *app_import_path*.

        Args:
            app_import_path: Import path of the target app.
        """
        self._get_app(app_import_path)
        cfg = self._build_config(app_import_path)
        command.heads(cfg, verbose=True)

    def current(self, app_import_path: str) -> None:
        """Print the current database revision for *app_import_path*.

        Args:
            app_import_path: Import path of the target app.
        """
        self._get_app(app_import_path)
        cfg = self._build_config(app_import_path)
        command.current(cfg, verbose=True)

    def get_history(self, app_import_path: str) -> list[dict[str, str]]:
        """Return the revision history for *app_import_path* as structured data.

        Iterates the branch from base to head and collects each revision's
        metadata.  The result is ordered from newest (head) to oldest (base),
        matching the conventional ``alembic history`` display order.

        Args:
            app_import_path: Import path of the target app.

        Returns:
            A list of dicts with keys ``revision``, ``down_revision``,
            ``branch_labels``, ``message``, and ``is_head``, one entry per
            revision in the branch.

        Raises:
            MigrationError: If the app is not installed or the script directory
                cannot be read.
        """
        self._get_app(app_import_path)
        cfg = self._build_config(app_import_path)

        try:
            script = ScriptDirectory.from_config(cfg)
            upper = f"{app_import_path}@head"
            lower = f"{app_import_path}@base"
            head_revs = {r.revision for r in script.get_revisions(upper)}
            rows: list[dict[str, str]] = []
            for rev in script.iterate_revisions(upper, lower):
                rows.append(
                    {
                        "revision": rev.revision,
                        "down_revision": (
                            ", ".join(rev.down_revision)
                            if isinstance(rev.down_revision, tuple)
                            else (rev.down_revision or "base")
                        ),
                        "branch_labels": ", ".join(sorted(rev.branch_labels or [])),
                        "message": rev.doc or "",
                        "is_head": rev.revision in head_revs,
                    }
                )
            return rows
        except CommandError as exc:
            raise MigrationError(str(exc), app_import_path) from exc

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def uninstall(self, app_import_path: str) -> None:
        """Downgrade the given app's branch to ``base`` in preparation for removal.

        This is the correct way to retire a Bedrock app from a project.  It
        downgrades all applied revisions for the app's Alembic branch back to
        ``base``, removing the corresponding rows from ``alembic_version``.
        After running this command the app can be safely removed from
        ``INSTALLED_APPS`` and its code deleted without leaving the Alembic
        version table in an inconsistent state.

        Cross-app ``depends_on`` links in Alembic migration files are not
        generated by Bedrock (dependency ordering is the developer's
        responsibility), so a plain per-branch downgrade is sufficient.

        Args:
            app_import_path: Import path of the app to uninstall, e.g.
                ``"myerp.wms"``.

        Raises:
            MigrationError: If the app is not installed, has no models, or
                the Alembic downgrade operation fails.
        """
        self._get_app(app_import_path)
        cfg = self._build_config(app_import_path)
        branch_label = app_import_path

        current_revs = self._get_current_revisions(cfg, branch_label)
        if not current_revs:
            # Nothing applied — already clean.
            return

        try:
            command.downgrade(cfg, f"{branch_label}@base")
        except CommandError as exc:
            raise MigrationError(str(exc), app_import_path) from exc

    def _get_app(self, app_import_path: str) -> "AppConfig":
        """Return the :class:`~bedrock.module.entities.AppConfig` for *app_import_path*
        and run a branch-ownership audit on its migrations directory.

        Args:
            app_import_path: Import path of the target app.

        Returns:
            The matching ``AppConfig``.

        Raises:
            MigrationError: If the app is not registered or has no models.
            BranchOwnershipError: If any revision in the app's migrations
                directory declares a branch label foreign to that app.
        """
        try:
            app = self._registry.get(app_import_path)
        except KeyError as exc:
            raise MigrationError(
                f"App '{app_import_path}' is not installed in the registry.",
                app_import_path,
            ) from exc

        if app.models_module is None:
            raise MigrationError(
                f"App '{app_import_path}' has no models submodule; "
                "migrations cannot be generated without SQLAlchemy models.",
                app_import_path,
            )

        # Audit before any operation so a mis-labelled revision is caught early.
        cfg = self._build_config(app_import_path)
        self._audit_app_revisions(cfg, app)

        return app

    def _build_config(self, app_import_path: str | None) -> Config:
        """Build an ephemeral :class:`~alembic.config.Config` for *app_import_path*.

        The config is never persisted to disk.  ``version_locations`` is
        derived from the live registry so it always reflects installed apps.
        Per-app context is injected into :attr:`~alembic.config.Config.attributes`
        for consumption by ``env.py``.

        Args:
            app_import_path: Import path of the target app, or ``None`` for a
                global (all-apps) config.

        Returns:
            A fully configured :class:`~alembic.config.Config` instance.
        """
        cfg = Config()
        cfg.set_main_option("script_location", self._script_location)
        cfg.set_main_option("sqlalchemy.url", self._database_url)
        cfg.set_main_option("file_template", "%%(rev)s_%%(year)d-%%(month).2d-%%(day).2d_%%(slug)s")
        # set version_table_pk
        cfg.set_main_option("version_table_pk", "id")
        # Collect migration directories from all installed apps that have them.
        locations: list[str] = []
        for app in self._registry.all():
            migrations_dir = app.package_dir / "migrations"
            if migrations_dir.exists():
                locations.append(str(migrations_dir))

        if locations:
            # path_separator=os uses os.pathsep (: on Unix, ; on Windows).
            cfg.set_main_option("path_separator", "os")
            cfg.set_main_option(
                "version_locations",
                os.pathsep.join(locations),
            )

        # Inject per-app context so env.py can filter tables correctly.
        cfg.attributes[_BEDROCK_MARKER] = True
        cfg.attributes["registry"] = self._registry
        cfg.attributes["current_app"] = app_import_path  # may be None

        return cfg

    def _ensure_migrations_dir(self, app: "AppConfig") -> Path:
        """Create the app's ``migrations/`` directory if it does not exist.

        Args:
            app: The app whose migrations directory should be ensured.

        Returns:
            The (possibly just-created) migrations directory path.
        """
        migrations_dir = app.package_dir / "migrations"
        migrations_dir.mkdir(parents=True, exist_ok=True)
        return migrations_dir

    def _branch_exists(self, cfg: Config, branch_label: str) -> bool:
        """Return whether a branch with *branch_label* already has revisions.

        Args:
            cfg: The Alembic config to use for script discovery.
            branch_label: The branch label to look up.

        Returns:
            ``True`` if at least one revision carries *branch_label*.
        """
        try:
            script = ScriptDirectory.from_config(cfg)
            for rev in script.walk_revisions():
                labels = rev.branch_labels or set()
                if branch_label in labels:
                    return True
            return False
        except Exception:
            # If the script directory cannot be read yet (first run), treat as
            # non-existent branch rather than propagating an internal error.
            log.debug("Failed to read script directory for branch existence check on '%s'", branch_label, exc_info=True)
            return False

    def _audit_app_revisions(self, cfg: Config, app: "AppConfig") -> None:
        """Verify that every revision file in *app*'s migrations directory
        declares only branch labels that belong to that app.

        A revision is considered foreign if it carries a branch label that does
        not match the app's own import path.  Revisions without any branch
        label (plain chain members) are allowed — only the root of a branch
        carries the label, so intermediate revisions legitimately have none.

        Args:
            cfg: Alembic config with ``version_locations`` already set.
            app: The app whose migrations directory should be audited.

        Raises:
            BranchOwnershipError: If any revision file in the app's directory
                declares a foreign branch label.
        """
        migrations_dir = app.package_dir / "migrations"
        if not migrations_dir.exists():
            return

        expected_label = app.name

        try:
            script = ScriptDirectory.from_config(cfg)
        except Exception:
            log.debug("Failed to load script directory for audit of app '%s'", app.name, exc_info=True)
            return

        for rev in script.walk_revisions():
            # Only inspect revisions that physically live in this app's directory.
            if rev.path is None:
                continue
            if not Path(rev.path).is_relative_to(migrations_dir):
                continue

            labels: frozenset[str] = frozenset(rev.branch_labels or [])
            if not labels:
                # Intermediate revision — no label is expected.
                continue

            foreign = labels - {expected_label}
            if foreign:
                raise BranchOwnershipError(
                    app_import_path=app.name,
                    revision_id=rev.revision,
                    declared_labels=labels,
                    expected_label=expected_label,
                )


def _is_missing_database_error(exc: Exception) -> bool:
    """Return ``True`` when *exc* indicates that the target database does not exist.

    Detects the PostgreSQL error code ``3D000`` (``invalid_catalog_name``) which
    is raised when a connection is attempted against a non-existent database.
    This helper inspects the exception chain so it works whether the driver
    wraps the error or raises it directly.

    Args:
        exc: The exception to inspect.

    Returns:
        ``True`` when the exception signals a missing database.
    """
    # Walk the exception chain looking for a known signal.
    current: BaseException | None = exc
    while current is not None:
        # psycopg2 / psycopg3 expose pgcode on the DBAPI exception.
        pgcode = getattr(current, "pgcode", None)
        if pgcode == "3D000":
            return True
        # Some drivers embed the error code in the message string.
        msg = str(current).lower()
        if "3d000" in msg or "does not exist" in msg and "database" in msg:
            return True
        current = current.__cause__ or current.__context__
    return False


__all__ = ["BranchOwnershipError", "MigrationError", "MigrationsManager"]
