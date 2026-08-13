from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
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

    @pytest.mark.parametrize("name", ["../pwn", r"bad\\name", "bad\nname", "bad:name", "123project"])
    def test_rejects_unsafe_or_invalid_package_name(self, name: str) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = runner.invoke(app, ["init", name, "-o", tmpdir])
            assert result.exit_code == 2
            assert "Invalid value" in result.output
            assert not (Path(tmpdir).parent / "pwn").exists()


def test_init_succeeds_when_output_dir_is_a_symlink(tmp_path) -> None:
    """A symlinked output dir must not crash the success-print loop (macOS /var -> /private/var)."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    from bedrock_cli.main import app
    from typer.testing import CliRunner

    result = CliRunner().invoke(app, ["init", "myapp", "-o", str(link)])
    assert result.exit_code == 0, result.output
    assert (real / "myapp" / "pyproject.toml").exists()
