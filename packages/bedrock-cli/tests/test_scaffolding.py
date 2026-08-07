"""Tests for the scaffolding engine."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from bedrock_cli.scaffolding import (
    RenderedFile,
    ScaffoldExistsError,
    ScaffoldPathError,
    render_files,
)


class TestRenderedFile:
    def test_creation(self) -> None:
        rf = RenderedFile("foo.py", "foo.py.j2", {"name": "test"})
        assert rf.relative_path == "foo.py"
        assert rf.template_name == "foo.py.j2"
        assert rf.context == {"name": "test"}

    def test_immutable(self) -> None:
        rf = RenderedFile("foo.py", "foo.py.j2", {"name": "test"})
        with pytest.raises(Exception):  # noqa: B017
            rf.relative_path = "bar.py"  # type: ignore[misc]


class TestRenderFiles:
    def test_renders_to_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            dest = Path(tmpdir) / "out"
            files = [
                RenderedFile("__init__.py", "module/__init__.py.j2", {"module_name": "test"}),
            ]
            paths = render_files(files, dest, overwrite=False)
            assert len(paths) == 1
            assert paths[0].exists()

    def test_creates_nested_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            dest = Path(tmpdir) / "deep" / "nested"
            files = [
                RenderedFile("sub/file.py", "module/__init__.py.j2", {"module_name": "x"}),
            ]
            render_files(files, dest, overwrite=False)
            assert (dest / "sub" / "file.py").exists()

    def test_rejects_non_empty_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            dest = Path(tmpdir)
            (dest / "existing.txt").write_text("hello", encoding="utf-8")
            with pytest.raises(ScaffoldExistsError):
                render_files(
                    files=[RenderedFile("new.py", "module/__init__.py.j2", {"module_name": "x"})],
                    destination=dest,
                    overwrite=False,
                )

    def test_allows_non_empty_with_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            dest = Path(tmpdir)
            (dest / "existing.txt").write_text("old", encoding="utf-8")
            paths = render_files(
                files=[RenderedFile("test.py", "module/__init__.py.j2", {"module_name": "t"})],
                destination=dest,
                overwrite=True,
            )
            assert len(paths) == 1
            assert paths[0].exists()

    def test_refuses_overwrite_single_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            dest = Path(tmpdir) / "target"
            dest.mkdir()
            (dest / "exists.py").write_text("x", encoding="utf-8")
            with pytest.raises(ScaffoldExistsError):
                render_files(
                    files=[RenderedFile("exists.py", "module/__init__.py.j2", {"module_name": "x"})],
                    destination=dest,
                    overwrite=False,
                )

    def test_overwrites_single_file_with_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            dest = Path(tmpdir) / "target"
            dest.mkdir()
            target = dest / "__init__.py"
            target.write_text("old content", encoding="utf-8")
            render_files(
                files=[RenderedFile("__init__.py", "module/__init__.py.j2", {"module_name": "new"})],
                destination=dest,
                overwrite=True,
            )
            assert target.read_text(encoding="utf-8") != "old content"

    @pytest.mark.parametrize(
        "relative_path", ["../escaped.py", "/tmp/escaped.py", r"..\\escaped.py", r"nested\\file.py"]
    )
    def test_rejects_paths_that_are_not_safe_relative_paths(self, relative_path: str) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            destination = Path(tmpdir) / "destination"
            with pytest.raises(ScaffoldPathError):
                render_files(
                    [RenderedFile(relative_path, "module/__init__.py.j2", {"module_name": "safe"})],
                    destination,
                )
            assert not (Path(tmpdir) / "escaped.py").exists()

    def test_rejects_existing_parent_symlink_that_escapes_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            destination = root / "destination"
            destination.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (destination / "linked").symlink_to(outside, target_is_directory=True)

            with pytest.raises(ScaffoldPathError):
                render_files(
                    [RenderedFile("linked/escaped.py", "module/__init__.py.j2", {"module_name": "safe"})],
                    destination,
                )
            assert not (outside / "escaped.py").exists()
