"""Create importable temporary Bedrock modules for tests."""

import importlib
import sys
import textwrap
import warnings
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
import yaml

from ..database.base import BedrockModel


class ModuleFactory:
    """Create importable temporary Bedrock modules under one root on sys.path.

    Args:
        root: Directory where module packages are created.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._created: set[str] = set()

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
        """Remove created packages and their models from process-global registries."""
        mappers = [
            mapper
            for mapper in BedrockModel.registry.mappers
            if mapper.class_.__module__.split(".")[0] in self._created
        ]
        dispose = getattr(BedrockModel.registry, "_dispose_cls", None)
        for mapper in mappers:
            BedrockModel.metadata.remove(mapper.local_table)
            if dispose is not None:
                dispose(mapper.class_)
        if mappers and dispose is None:
            warnings.warn(
                "Could not dispose model classes; redefining models in the next test may warn",
                UserWarning,
                stacklevel=2,
            )
        for name in list(sys.modules):
            if name.split(".")[0] in self._created:
                del sys.modules[name]
        importlib.invalidate_caches()


@pytest.fixture
def module_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleFactory]:
    """Yield a factory for importable temporary modules and clean it up."""
    root = tmp_path / "bedrock_modules"
    root.mkdir()
    monkeypatch.syspath_prepend(str(root))
    factory = ModuleFactory(root)
    try:
        yield factory
    finally:
        factory.cleanup()
