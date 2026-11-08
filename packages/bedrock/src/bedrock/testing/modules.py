"""Create importable temporary Bedrock modules for tests."""

import importlib
import sys
import textwrap
import warnings
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
import yaml
from sqlalchemy import Table, event

from ..database.base import BedrockModel
from .runtime import _defer_until_runtime_shutdown


class ModuleFactory:
    """Create importable temporary Bedrock modules under one root on sys.path.

    While the factory is open it records every table its packages attach to ``BedrockModel.metadata`` at import time,
    including unmapped ``Table`` objects such as many-to-many association tables, so cleanup can drop them. A table
    belongs to the module whose top-level code is closest on the call stack, so tables of application modules that a
    temporary module merely imports stay registered.

    Call :meth:`cleanup` when done: it also removes the factory's SQLAlchemy event listener. The ``module_factory``
    fixture does this for you.

    Args:
        root: Directory where module packages are created.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._created: set[str] = set()
        self._tables: list[Table] = []
        self._closed = False
        event.listen(Table, "after_parent_attach", self._record_table)

    def _record_table(self, table: Table, parent: object) -> None:
        """Remember ``table`` when one of this factory's modules defines it on the shared metadata."""
        if parent is not BedrockModel.metadata:
            return
        frame = sys._getframe(1)
        while frame is not None and frame.f_code.co_name != "<module>":
            frame = frame.f_back
        if frame is not None and str(frame.f_globals.get("__name__", "")).split(".")[0] in self._created:
            self._tables.append(table)

    @property
    def root(self) -> Path:
        """Return the directory containing the created packages."""
        return self._root

    def __call__(
        self,
        name: str,
        *,
        manifest: Mapping[str, object] | None = None,
        bootstrap: str | None = None,
        models: str | None = None,
        files: Mapping[str, str] | None = None,
    ) -> str:
        """Create a module package and return its import path.

        Args:
            name: Python identifier used as the package name.
            manifest: YAML-serializable manifest, or None for the default.
            bootstrap: Optional bootstrap source code.
            models: Optional model source code.
            files: Additional relative paths and their source code.

        Returns:
            The package import path.

        Raises:
            ValueError: If the name is invalid or already created by this factory.
        """
        if not name.isidentifier():
            raise ValueError(f"Module name {name!r} must be a plain Python identifier without dots.")
        if name in self._created:
            raise ValueError(f"Module {name!r} was already created in this test.")
        self._created.add(name)
        package = self._root / name
        package.mkdir(parents=True, exist_ok=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        data = dict(manifest) if manifest is not None else {"title": name, "version": "0.1.0"}
        (package / "manifest.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
        sources = dict(files or {})
        if bootstrap is not None:
            sources["bootstrap.py"] = bootstrap
        if models is not None:
            sources["models.py"] = models
        for filename, source in sources.items():
            path = package / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(source), encoding="utf-8")
        return name

    def cleanup(self) -> None:
        """Remove created packages and their models from process-global registries.

        Calling it again does nothing.
        """
        if self._closed:
            return
        self._closed = True
        event.remove(Table, "after_parent_attach", self._record_table)
        mappers = [
            mapper
            for mapper in BedrockModel.registry.mappers
            if mapper.class_.__module__.split(".")[0] in self._created
        ]
        dispose = getattr(BedrockModel.registry, "_dispose_cls", None)
        for mapper in mappers:
            if dispose is not None:
                dispose(mapper.class_)
        if mappers and dispose is None:
            warnings.warn(
                "Could not dispose model classes; redefining models in the next test may warn",
                UserWarning,
                stacklevel=2,
            )
        metadata = BedrockModel.metadata
        for table in [*(mapper.local_table for mapper in mappers), *self._tables]:
            if metadata.tables.get(table.key) is table:
                metadata.remove(table)
        for name in list(sys.modules):
            if name.split(".")[0] in self._created:
                del sys.modules[name]
        importlib.invalidate_caches()


@pytest.fixture
def module_factory(tmp_path: Path) -> Iterator[ModuleFactory]:
    """Yield a factory for importable temporary modules and clean it up.

    When a runtime that is still open at teardown holds modules from this factory that are not shut down yet, cleanup
    waits until that runtime has shut them down, so ``on_shutdown`` hooks can still import from the temporary packages
    whatever order the fixtures were requested in. Otherwise cleanup runs at once.
    """
    root = tmp_path / "bedrock_modules"
    root.mkdir()
    sys.path.insert(0, str(root))
    importlib.invalidate_caches()
    factory = ModuleFactory(root)

    def cleanup() -> None:
        try:
            factory.cleanup()
        finally:
            if str(root) in sys.path:
                sys.path.remove(str(root))
            importlib.invalidate_caches()

    try:
        yield factory
    finally:
        if not _defer_until_runtime_shutdown(cleanup, factory._created):
            cleanup()
