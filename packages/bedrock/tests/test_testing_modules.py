"""Tests for temporary Bedrock module creation and cleanup."""

import importlib
import sys
import warnings
from pathlib import Path

import pytest
import yaml
from bedrock.database.base import BedrockModel
from bedrock.module import ModuleRegistry
from bedrock.testing.modules import ModuleFactory


def test_factory_writes_dedented_sources_and_default_manifest(module_factory: ModuleFactory) -> None:
    name = module_factory(
        "factory_files",
        bootstrap="\n    VALUE = 1\n",
        models="\n    VALUE = 2\n",
        files={"nested/source.py": "\n    VALUE = 3\n"},
    )
    package = module_factory.root / name
    assert (package / "__init__.py").read_text() == ""
    assert yaml.safe_load((package / "manifest.yaml").read_text()) == {"title": name, "version": "0.1.0"}
    assert (package / "bootstrap.py").read_text() == "\nVALUE = 1\n"
    assert (package / "models.py").read_text() == "\nVALUE = 2\n"
    assert (package / "nested/source.py").read_text() == "\nVALUE = 3\n"


def test_factory_module_installs(module_factory: ModuleFactory) -> None:
    name = module_factory("factory_install", manifest={"title": "Custom", "version": "1.2.3"})
    app = ModuleRegistry().install(name)
    assert app.name == name
    assert app.manifest.title == "Custom"


def test_cleanup_allows_reusing_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    monkeypatch.syspath_prepend(str(first_root))
    first = ModuleFactory(first_root)
    first("factory_reuse", bootstrap="VALUE = 'first'\n")
    assert importlib.import_module("factory_reuse.bootstrap").VALUE == "first"
    first.cleanup()
    assert "factory_reuse" not in sys.modules
    assert "factory_reuse.bootstrap" not in sys.modules
    monkeypatch.syspath_prepend(str(second_root))
    second = ModuleFactory(second_root)
    try:
        second("factory_reuse", bootstrap="VALUE = 'second'\n")
        assert importlib.import_module("factory_reuse.bootstrap").VALUE == "second"
    finally:
        second.cleanup()


def test_cleanup_allows_redefining_model_without_warnings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = """
        from sqlalchemy.orm import Mapped, mapped_column
        from bedrock.database.base import BedrockModel

        class FactoryModel(BedrockModel):
            __tablename__ = "factory_test_table"
            id: Mapped[int] = mapped_column(primary_key=True)
    """
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        for index in range(2):
            root = tmp_path / str(index)
            root.mkdir()
            monkeypatch.syspath_prepend(str(root))
            factory = ModuleFactory(root)
            try:
                factory("factory_model", models=source)
                importlib.import_module("factory_model.models")
                assert "factory_test_table" in BedrockModel.metadata.tables
            finally:
                factory.cleanup()
            assert "factory_test_table" not in BedrockModel.metadata.tables


def test_cleanup_removes_unmapped_association_tables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = """
        from sqlalchemy import Column, ForeignKey, Table
        from sqlalchemy.orm import Mapped, mapped_column, relationship
        from bedrock.database.base import BedrockModel

        factory_tags = Table(
            "factory_post_tags",
            BedrockModel.metadata,
            Column("post_id", ForeignKey("factory_posts.id"), primary_key=True),
            Column("tag_id", ForeignKey("factory_tags.id"), primary_key=True),
        )

        class FactoryTag(BedrockModel):
            __tablename__ = "factory_tags"
            id: Mapped[int] = mapped_column(primary_key=True)

        class FactoryPost(BedrockModel):
            __tablename__ = "factory_posts"
            id: Mapped[int] = mapped_column(primary_key=True)
            tags = relationship(FactoryTag, secondary=factory_tags)
    """
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        for index in range(2):
            root = tmp_path / str(index)
            root.mkdir()
            monkeypatch.syspath_prepend(str(root))
            factory = ModuleFactory(root)
            try:
                factory("factory_m2m", models=source)
                importlib.import_module("factory_m2m.models")
            finally:
                factory.cleanup()
            assert not {"factory_post_tags", "factory_tags", "factory_posts"} & set(BedrockModel.metadata.tables)


def test_rejects_invalid_and_duplicate_names(module_factory: ModuleFactory) -> None:
    for name in ("invalid.name", "invalid-name", "", "1invalid"):
        with pytest.raises(ValueError):
            module_factory(name)
    module_factory("factory_duplicate")
    with pytest.raises(ValueError):
        module_factory("factory_duplicate")
