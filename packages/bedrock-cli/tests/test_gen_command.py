from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from bedrock_cli.commands.gen import (
    _build_context,
    _parse_model_ref,
    _resolve_template_name,
    _slugify,
)
from bedrock_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()


class TestSlugify:
    def test_hyphen(self) -> None:
        assert _slugify("my-module") == "my_module"

    def test_spaces(self) -> None:
        assert _slugify("My Module") == "my_module"

    def test_already_valid(self) -> None:
        assert _slugify("inventory") == "inventory"

    def test_mixed(self) -> None:
        assert _slugify("User-Auth Module") == "user_auth_module"


class TestParseModelRef:
    def test_valid_ref(self) -> None:
        module_path, class_name = _parse_model_ref("myapp.models:User")
        assert module_path == "myapp.models"
        assert class_name == "User"

    def test_nested_module(self) -> None:
        module_path, class_name = _parse_model_ref("myapp.auth.models:AuthUser")
        assert module_path == "myapp.auth.models"
        assert class_name == "AuthUser"

    def test_invalid_ref_exits(self) -> None:
        from click.exceptions import Exit

        with pytest.raises(Exit):
            _parse_model_ref("no_colon_here")


class TestResolveTemplateName:
    def test_builtin_entity(self) -> None:
        result = _resolve_template_name("entity")
        assert result == "crud/entities.py.j2"

    def test_builtin_service(self) -> None:
        result = _resolve_template_name("service")
        assert result == "crud/service.py.j2"

    def test_unknown_template_exits(self) -> None:
        from click.exceptions import Exit

        with pytest.raises(Exit):
            _resolve_template_name("nonexistent_xyz_template")

    def test_user_template_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            user_dir = Path(tmpdir) / "_bedrock_gen"
            user_dir.mkdir()
            (user_dir / "router.py.j2").write_text("# router", encoding="utf-8")

            with patch("bedrock_cli.template_env.Path") as mock_path:
                mock_path.return_value.resolve.return_value = Path(tmpdir)
                result = _resolve_template_name("router")
                assert result == "router.py.j2"


class TestBuildContext:
    def _make_mock_model(self):
        col_id = MagicMock()
        col_id.name = "id"
        col_id.type = MagicMock()
        type(col_id.type).__name__ = "Integer"
        col_id.nullable = False
        col_id.primary_key = True

        col_name = MagicMock()
        col_name.name = "name"
        col_name.type = MagicMock()
        type(col_name.type).__name__ = "String"
        col_name.nullable = False
        col_name.primary_key = False

        col_email = MagicMock()
        col_email.name = "email"
        col_email.type = MagicMock()
        type(col_email.type).__name__ = "String"
        col_email.nullable = True
        col_email.primary_key = False

        model_cls = MagicMock()
        model_cls.__name__ = "User"
        model_cls.__table__ = MagicMock()
        model_cls.__table__.columns = [col_id, col_name, col_email]

        return model_cls

    @patch("bedrock_cli.commands.gen._ensure_importable")
    @patch("bedrock_cli.commands.gen._resolve_and_load")
    @patch("bedrock_cli.commands.gen.find_spec")
    def test_builds_context(self, mock_find_spec, mock_resolve, mock_ensure) -> None:
        mock_find_spec.return_value = MagicMock()
        mock_resolve.return_value = self._make_mock_model()

        context = _build_context("myapp.models:User", "user")

        assert context["model_name"] == "User"
        assert context["model_slug"] == "user"
        assert context["module_name"] == "user"
        assert len(context["columns"]) == 3
        assert len(context["pk_columns"]) == 1
        assert len(context["data_columns"]) == 2
        assert context["pk_columns"][0]["name"] == "id"
        assert context["needs_datetime"] is False

    @patch("bedrock_cli.commands.gen._ensure_importable")
    @patch("bedrock_cli.commands.gen._resolve_and_load")
    @patch("bedrock_cli.commands.gen.find_spec")
    def test_nullable_columns(self, mock_find_spec, mock_resolve, mock_ensure) -> None:
        mock_find_spec.return_value = MagicMock()
        mock_resolve.return_value = self._make_mock_model()

        context = _build_context("myapp.models:User", "user")

        email_col = next(c for c in context["data_columns"] if c["name"] == "email")
        assert email_col["nullable"] is True


class TestGenCommandIntegration:
    @patch("bedrock_cli.commands.gen._build_context")
    def test_gen_entity_writes_file(self, mock_context) -> None:
        mock_context.return_value = {
            "model_name": "User",
            "model_slug": "user",
            "module_name": "user",
            "columns": [
                {
                    "name": "id",
                    "python_type": "int",
                    "nullable": False,
                    "primary_key": True,
                    "needs_datetime_import": False,
                    "needs_decimal_import": False,
                }
            ],
            "data_columns": [],
            "pk_columns": [
                {
                    "name": "id",
                    "python_type": "int",
                    "nullable": False,
                    "primary_key": True,
                    "needs_datetime_import": False,
                    "needs_decimal_import": False,
                }
            ],
            "needs_datetime": False,
            "needs_any": False,
            "needs_decimal": False,
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            result = runner.invoke(app, ["gen", "entity", "myapp.models:User", tmpdir])
            assert result.exit_code == 0
            assert (Path(tmpdir) / "entity.py").exists()

    @patch("bedrock_cli.commands.gen._build_context")
    def test_gen_service_writes_file(self, mock_context) -> None:
        mock_context.return_value = {
            "model_name": "User",
            "model_slug": "user",
            "module_name": "user",
            "columns": [
                {
                    "name": "id",
                    "python_type": "int",
                    "nullable": False,
                    "primary_key": True,
                    "needs_datetime_import": False,
                    "needs_decimal_import": False,
                }
            ],
            "data_columns": [],
            "pk_columns": [
                {
                    "name": "id",
                    "python_type": "int",
                    "nullable": False,
                    "primary_key": True,
                    "needs_datetime_import": False,
                    "needs_decimal_import": False,
                }
            ],
            "needs_datetime": False,
            "needs_any": False,
            "needs_decimal": False,
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            result = runner.invoke(app, ["gen", "service", "myapp.models:User", tmpdir])
            assert result.exit_code == 0
            assert (Path(tmpdir) / "service.py").exists()

    @patch("bedrock_cli.commands.gen._build_context")
    def test_gen_with_name_override(self, mock_context) -> None:
        mock_context.return_value = {
            "model_name": "User",
            "model_slug": "account",
            "module_name": "account",
            "columns": [],
            "data_columns": [],
            "pk_columns": [],
            "needs_datetime": False,
            "needs_any": False,
            "needs_decimal": False,
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            result = runner.invoke(app, ["gen", "entity", "myapp.models:User", tmpdir, "-n", "account"])
            assert result.exit_code == 0

    def test_gen_unknown_template_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = runner.invoke(app, ["gen", "nonexistent_xyz", "myapp.models:User", tmpdir])
            assert result.exit_code == 1

    @patch("bedrock_cli.commands.gen._build_context")
    def test_gen_refuses_overwrite(self, mock_context) -> None:
        mock_context.return_value = {
            "model_name": "User",
            "model_slug": "user",
            "module_name": "user",
            "columns": [],
            "data_columns": [],
            "pk_columns": [],
            "needs_datetime": False,
            "needs_any": False,
            "needs_decimal": False,
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "entity.py").write_text("existing", encoding="utf-8")
            result = runner.invoke(app, ["gen", "entity", "myapp.models:User", tmpdir])
            assert result.exit_code == 1

    @patch("bedrock_cli.commands.gen._build_context")
    def test_gen_overwrite_flag(self, mock_context) -> None:
        mock_context.return_value = {
            "model_name": "User",
            "model_slug": "user",
            "module_name": "user",
            "columns": [],
            "data_columns": [],
            "pk_columns": [],
            "needs_datetime": False,
            "needs_any": False,
            "needs_decimal": False,
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "entity.py").write_text("old", encoding="utf-8")
            result = runner.invoke(app, ["gen", "entity", "myapp.models:User", tmpdir, "--overwrite"])
            assert result.exit_code == 0
            assert (Path(tmpdir) / "entity.py").read_text(encoding="utf-8") != "old"
