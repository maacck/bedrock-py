from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from bedrock_cli.template_env import _to_camel, _to_snake, build_template_environment


class TestSnakeFilter:
    def test_pascal_case(self) -> None:
        assert _to_snake("MyModule") == "my_module"

    def test_all_caps(self) -> None:
        assert _to_snake("HTTP") == "http"

    def test_camel_with_acronym(self) -> None:
        assert _to_snake("HTTPResponse") == "http_response"

    def test_already_snake(self) -> None:
        assert _to_snake("already_snake") == "already_snake"

    def test_mixed_digits(self) -> None:
        assert _to_snake("Model2D") == "model2_d"


class TestCamelFilter:
    def test_simple(self) -> None:
        assert _to_camel("my_module") == "MyModule"

    def test_single_word(self) -> None:
        assert _to_camel("user") == "User"

    def test_kebab_case(self) -> None:
        assert _to_camel("order-item") == "OrderItem"

    def test_empty_parts(self) -> None:
        assert _to_camel("_leading") == "Leading"


class TestBuildTemplateEnvironment:
    def test_creates_environment(self) -> None:
        env = build_template_environment()
        assert env is not None

    def test_has_filters(self) -> None:
        env = build_template_environment()
        assert "snake" in env.filters
        assert "camel" in env.filters

    def test_loads_builtin_templates(self) -> None:
        env = build_template_environment()
        template = env.get_template("module/__init__.py.j2")
        assert template is not None

    def test_user_template_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            user_dir = Path(tmpdir) / "_bedrock_gen"
            user_dir.mkdir()
            (user_dir / "custom.py.j2").write_text("# custom: {{ model_name }}", encoding="utf-8")

            with patch("bedrock_cli.template_env.Path") as mock_path:
                mock_resolve = Path(tmpdir)
                mock_path.return_value.resolve.return_value = mock_resolve

                from bedrock_cli.template_env import _USER_TEMPLATE_DIR
                from jinja2 import ChoiceLoader, Environment, FileSystemLoader, PackageLoader

                user_path = mock_resolve / _USER_TEMPLATE_DIR
                assert user_path.is_dir()

                loader = ChoiceLoader([FileSystemLoader(str(user_path)), PackageLoader("bedrock_cli", "templates")])
                env = Environment(loader=loader, autoescape=False, keep_trailing_newline=True)
                result = env.get_template("custom.py.j2").render(model_name="Test")
                assert result == "# custom: Test"

    def test_fallback_without_user_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("bedrock_cli.template_env.Path") as mock_path:
                mock_path.return_value.resolve.return_value = Path(tmpdir)
                env = build_template_environment()
                template = env.get_template("module/__init__.py.j2")
                assert template is not None
