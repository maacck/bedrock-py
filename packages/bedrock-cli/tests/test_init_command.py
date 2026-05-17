from __future__ import annotations

import tempfile
from pathlib import Path

from bedrock_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()


class TestInitCommand:
    def test_creates_project_structure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = runner.invoke(app, ["init", "my-app", "-o", tmpdir])
            assert result.exit_code == 0

            project_dir = Path(tmpdir) / "my-app"
            assert project_dir.exists()
            assert (project_dir / "pyproject.toml").exists()
            assert (project_dir / ".python-version").exists()
            assert (project_dir / "README.md").exists()
            assert (project_dir / "src" / "my_app" / "__init__.py").exists()
            assert (project_dir / "src" / "my_app" / "manifest.yaml").exists()
            assert (project_dir / "src" / "my_app" / "models.py").exists()
            assert (project_dir / "src" / "my_app" / "bootstrap.py").exists()
            assert (project_dir / "src" / "my_app" / "installation.py").exists()
            assert (project_dir / "src" / "my_app" / "exc.py").exists()

    def test_slugifies_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = runner.invoke(app, ["init", "My Cool App", "-o", tmpdir])
            assert result.exit_code == 0

            project_dir = Path(tmpdir) / "My Cool App"
            assert (project_dir / "src" / "my_cool_app" / "__init__.py").exists()

    def test_fails_on_existing_non_empty_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "existing"
            project_dir.mkdir()
            (project_dir / "file.txt").write_text("x", encoding="utf-8")

            result = runner.invoke(app, ["init", "existing", "-o", tmpdir])
            assert result.exit_code == 1

    def test_overwrite_allows_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "my-app"
            project_dir.mkdir()
            (project_dir / "old.txt").write_text("x", encoding="utf-8")

            result = runner.invoke(app, ["init", "my-app", "-o", tmpdir, "--overwrite"])
            assert result.exit_code == 0
            assert (project_dir / "pyproject.toml").exists()

    def test_manifest_contains_module_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner.invoke(app, ["init", "test-proj", "-o", tmpdir])
            manifest = (Path(tmpdir) / "test-proj" / "src" / "test_proj" / "manifest.yaml").read_text(encoding="utf-8")
            assert "test_proj" in manifest
