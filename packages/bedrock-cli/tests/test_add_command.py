from __future__ import annotations

import tempfile
from pathlib import Path

from bedrock_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()


class TestAddSubmodule:
    def test_creates_submodule_structure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = runner.invoke(
                app, ["add", "submodule", "inventory", tmpdir, "--no-bootstrap", "--no-installation"]
            )
            assert result.exit_code == 0

            mod_dir = Path(tmpdir) / "inventory"
            assert (mod_dir / "__init__.py").exists()
            assert (mod_dir / "manifest.yaml").exists()
            assert (mod_dir / "models.py").exists()
            assert (mod_dir / "entities.py").exists()
            assert (mod_dir / "exc.py").exists()
            assert not (mod_dir / "bootstrap.py").exists()
            assert not (mod_dir / "installation.py").exists()

    def test_with_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = runner.invoke(app, ["add", "submodule", "auth", tmpdir, "--bootstrap", "--no-installation"])
            assert result.exit_code == 0
            assert (Path(tmpdir) / "auth" / "bootstrap.py").exists()

    def test_with_installation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = runner.invoke(app, ["add", "submodule", "auth", tmpdir, "--no-bootstrap", "--installation"])
            assert result.exit_code == 0
            assert (Path(tmpdir) / "auth" / "installation.py").exists()

    def test_slugifies_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = runner.invoke(
                app, ["add", "submodule", "User-Auth", tmpdir, "--no-bootstrap", "--no-installation"]
            )
            assert result.exit_code == 0
            assert (Path(tmpdir) / "user_auth" / "manifest.yaml").exists()

    def test_fails_on_existing_non_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mod_dir = Path(tmpdir) / "inventory"
            mod_dir.mkdir()
            (mod_dir / "existing.py").write_text("x", encoding="utf-8")

            result = runner.invoke(
                app, ["add", "submodule", "inventory", tmpdir, "--no-bootstrap", "--no-installation"]
            )
            assert result.exit_code == 1

    def test_overwrite_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mod_dir = Path(tmpdir) / "inventory"
            mod_dir.mkdir()
            (mod_dir / "old.py").write_text("x", encoding="utf-8")

            result = runner.invoke(
                app, ["add", "submodule", "inventory", tmpdir, "--no-bootstrap", "--no-installation", "--overwrite"]
            )
            assert result.exit_code == 0
            assert (mod_dir / "manifest.yaml").exists()

    def test_manifest_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runner.invoke(app, ["add", "submodule", "billing", tmpdir, "--no-bootstrap", "--no-installation"])
            manifest = (Path(tmpdir) / "billing" / "manifest.yaml").read_text(encoding="utf-8")
            assert "billing" in manifest
            assert "0.1.0" in manifest


class TestAddDomain:
    def test_creates_domain_structure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = runner.invoke(app, ["add", "domain", "user", tmpdir])
            assert result.exit_code == 0

            dom_dir = Path(tmpdir) / "user"
            assert (dom_dir / "__init__.py").exists()
            assert (dom_dir / "entities.py").exists()
            assert (dom_dir / "service.py").exists()
            assert (dom_dir / "exc.py").exists()
            assert not (dom_dir / "manifest.yaml").exists()
            assert not (dom_dir / "models.py").exists()

    def test_slugifies_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = runner.invoke(app, ["add", "domain", "Sales-Order", tmpdir])
            assert result.exit_code == 0
            assert (Path(tmpdir) / "sales_order" / "entities.py").exists()

    def test_fails_on_existing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            dom_dir = Path(tmpdir) / "user"
            dom_dir.mkdir()
            (dom_dir / "x.py").write_text("x", encoding="utf-8")

            result = runner.invoke(app, ["add", "domain", "user", tmpdir])
            assert result.exit_code == 1

    def test_overwrite_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            dom_dir = Path(tmpdir) / "user"
            dom_dir.mkdir()
            (dom_dir / "old.py").write_text("x", encoding="utf-8")

            result = runner.invoke(app, ["add", "domain", "user", tmpdir, "--overwrite"])
            assert result.exit_code == 0
            assert (dom_dir / "entities.py").exists()
