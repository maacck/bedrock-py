"""Tests for isolated Bedrock runtimes."""

from __future__ import annotations

import os
from pathlib import Path

import bedrock
import bedrock.lifecycle as lifecycle
import bedrock.module as module
import bedrock.module.registry as registry_module
import pytest
from bedrock.conf import SettingsProxy
from bedrock.database import db
from bedrock.di import container
from bedrock.module import ModuleLifecycleError, ModuleRegistry
from bedrock.module.signals import module_ready
from bedrock.testing.runtime import BedrockRuntime, LifecycleEvent, isolated_runtime
from pydantic_settings import BaseSettings
from sqlalchemy import text


def make_package(root: Path, name: str, bootstrap: str = "") -> str:
    """Create a minimal importable Bedrock module."""
    package = root / name
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "manifest.yaml").write_text(f'title: {name}\nversion: "1.0"\n')
    (package / "bootstrap.py").write_text(bootstrap)
    return name


def test_aliases_are_replaced_and_restored() -> None:
    owners = (bedrock, module, registry_module, lifecycle)
    originals = tuple(owner.apps for owner in owners)
    with isolated_runtime() as runtime:
        assert all(owner.apps is runtime.registry for owner in owners)
    assert all(owner.apps is original for owner, original in zip(owners, originals, strict=True))


def test_consecutive_runtimes_can_setup(fake_package: Path) -> None:
    name = make_package(fake_package, "runtime_consecutive")
    registries = []
    for _ in range(2):
        with isolated_runtime() as runtime:
            bedrock.setup(name)
            assert runtime.registry.ready
            assert runtime.registry.get(name).name == name
            registries.append(runtime.registry)
    assert registries[0] is not registries[1]


def test_events_only_record_runtime_registry(fake_package: Path, bedrock_runtime: BedrockRuntime) -> None:
    name = make_package(fake_package, "runtime_events")
    other = ModuleRegistry()
    other.populate([name])
    other.shutdown()
    assert bedrock_runtime.events == []
    bedrock.setup(name)
    bedrock_runtime.registry.shutdown()
    assert bedrock_runtime.events == [
        LifecycleEvent("loaded", name),
        LifecycleEvent("ready", name),
        LifecycleEvent("registry_ready", None),
        LifecycleEvent("shutdown", name),
        LifecycleEvent("registry_shutdown", None),
    ]


def test_teardown_shuts_down_modules(fake_package: Path, tmp_path: Path) -> None:
    marker = tmp_path / "shutdown"
    name = make_package(
        fake_package, "runtime_shutdown", f"def on_shutdown(*, app):\n    open({str(marker)!r}, 'w').write('done')\n"
    )
    with isolated_runtime():
        bedrock.setup(name)
    assert marker.read_text() == "done"


def test_teardown_shuts_down_installed_unready_module(fake_package: Path, tmp_path: Path) -> None:
    marker = tmp_path / "unready-shutdown"
    name = make_package(
        fake_package,
        "runtime_unready",
        f"def on_shutdown(*, app):\n    open({str(marker)!r}, 'w').write('done')\n",
    )
    with isolated_runtime() as runtime:
        runtime.registry.install(name)
    assert marker.read_text() == "done"


def test_teardown_reports_all_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    original = lifecycle.apps

    def fail_container() -> None:
        raise RuntimeError("container failure")

    def fail_hooks() -> None:
        raise ValueError("hooks failure")

    from bedrock.hooks import hooks

    monkeypatch.setattr(container, "reset", fail_container)
    monkeypatch.setattr(hooks, "reset", fail_hooks)
    with pytest.raises(RuntimeError, match="container failure") as error:
        with isolated_runtime():
            os.environ["BEDROCK_RUNTIME_ERRORS"] = "changed"
    assert any("hooks failure" in note for note in error.value.__notes__)
    assert lifecycle.apps is original
    assert "BEDROCK_RUNTIME_ERRORS" not in os.environ


def test_teardown_errors_do_not_mask_the_block_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_container() -> None:
        raise RuntimeError("container failure")

    monkeypatch.setattr(container, "reset", fail_container)
    with pytest.raises(AssertionError, match="test failure") as error:
        with isolated_runtime():
            raise AssertionError("test failure")
    assert any("container failure" in note for note in error.value.__notes__)


def test_signal_receivers_are_restored(fake_package: Path) -> None:
    name = make_package(
        fake_package,
        "runtime_receiver",
        "from bedrock.module.signals import module_ready\n"
        "def receiver(sender, **kwargs): pass\n"
        "def on_load(*, app): module_ready.connect(receiver, weak=False)\n",
    )
    original = dict(module_ready.receivers)
    with isolated_runtime():
        bedrock.setup(name)
        assert len(module_ready.receivers) > len(original)
    assert module_ready.receivers == original


def test_shutdown_error_does_not_prevent_restoration(fake_package: Path) -> None:
    name = make_package(
        fake_package, "runtime_failure", "def on_shutdown(*, app): raise RuntimeError('shutdown boom')\n"
    )
    original = lifecycle.apps
    key = "BEDROCK_RUNTIME_FAILURE_TEST"
    os.environ.pop(key, None)
    with pytest.raises(ModuleLifecycleError, match="shutdown boom"):
        with isolated_runtime():
            bedrock.setup(name)
            os.environ[key] = "changed"
            container.register_instance("runtime-test", object())
    assert key not in os.environ
    assert lifecycle.apps is original
    assert not container.is_registered("runtime-test")


def test_environment_is_restored() -> None:
    key = "BEDROCK_RUNTIME_ENV_TEST"
    os.environ.pop(key, None)
    with isolated_runtime():
        os.environ[key] = "changed"
    assert key not in os.environ


def test_settings_proxy_is_reset_at_both_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    class Settings(BaseSettings):
        value: str = "default"

    monkeypatch.setenv("VALUE", "before")
    proxy = SettingsProxy(Settings)
    assert proxy.value == "before"
    monkeypatch.setenv("VALUE", "inside")
    with isolated_runtime():
        assert proxy.value == "inside"
        os.environ["VALUE"] = "changed"
    assert proxy.value == "inside"


def test_teardown_leaves_global_database_open(fake_package: Path, tmp_path: Path) -> None:
    name = make_package(fake_package, "runtime_database")
    db.init(url=f"sqlite:///{tmp_path / 'runtime.db'}")
    try:
        with isolated_runtime():
            bedrock.setup(name)
        assert db.session.execute(text("select 1")).scalar_one() == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_async_lifecycle_events(fake_package: Path) -> None:
    name = make_package(fake_package, "runtime_async")
    with isolated_runtime() as runtime:
        await bedrock.asetup(name)
        await bedrock.ashutdown()
        assert runtime.events == [
            LifecycleEvent("loaded", name),
            LifecycleEvent("ready", name),
            LifecycleEvent("registry_ready", None),
            LifecycleEvent("shutdown", name),
            LifecycleEvent("registry_shutdown", None),
        ]
