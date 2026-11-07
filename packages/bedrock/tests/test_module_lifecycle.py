"""Failure-atomic install, retry-safe readiness, and resilient shutdown of ``ModuleRegistry``."""

from __future__ import annotations

import builtins
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from bedrock.module import (
    AppConfig,
    ModuleLifecycleError,
    ModuleLifecycleState,
    ModuleRegistry,
    ModuleStartupRollbackError,
    build_app_config,
)
from bedrock.module.signals import module_loaded, module_ready, module_shutdown, registry_ready, registry_shutdown
from pydantic import ValidationError

from tests.helpers import make_fake_module

_BOOTSTRAP = """
import builtins


def _record(hook, app, registry):
    builtins._lifecycle_calls.append(f"{hook}:{app.name}")
    reenter = builtins._lifecycle_reenter.get((hook, app.name))
    if reenter is not None:
        method, _, argument = reenter.partition(":")
        getattr(registry, method)(*([argument] if argument else []))
    if (hook, app.name) in builtins._lifecycle_failures:
        raise RuntimeError(f"{hook} boom in {app.name}")


def on_load(*, app, registry):
    _record("on_load", app, registry)


def ready(*, app, registry):
    _record("ready", app, registry)


def on_shutdown(*, app, registry):
    _record("on_shutdown", app, registry)
"""


@pytest.fixture(autouse=True)
def lifecycle_log(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every hook call as ``"<hook>:<module>"`` and let tests choose failing hooks."""
    calls: list[str] = []
    monkeypatch.setattr(builtins, "_lifecycle_calls", calls, raising=False)
    monkeypatch.setattr(builtins, "_lifecycle_failures", set(), raising=False)
    monkeypatch.setattr(builtins, "_lifecycle_reenter", {}, raising=False)
    return calls


@pytest.fixture
def fail_hooks() -> set[tuple[str, str]]:
    """Mutable set of ``(hook, module)`` pairs whose hook currently raises."""
    return builtins._lifecycle_failures  # type: ignore[attr-defined, no-any-return]


@pytest.fixture
def reenter_hooks() -> dict[tuple[str, str], str]:
    """Mutable map of ``(hook, module)`` to the registry call that hook makes.

    A value is a method name such as ``"shutdown"``, or ``"install:<import path>"`` to pass an argument.
    """
    return builtins._lifecycle_reenter  # type: ignore[attr-defined, no-any-return]


@pytest.fixture
def signal_log() -> Iterator[list[tuple[str, str | None]]]:
    """Collect lifecycle signals as ``(signal, module_name)`` tuples for the registry under test."""
    events: list[tuple[str, str | None]] = []
    signals = {
        "loaded": module_loaded,
        "ready": module_ready,
        "shutdown": module_shutdown,
        "registry_ready": registry_ready,
        "registry_shutdown": registry_shutdown,
    }
    receivers = []
    for label, signal in signals.items():

        def receiver(sender: Any, _label: str = label, **kwargs: Any) -> None:
            module = kwargs.get("module")
            events.append((_label, module.name if module is not None else None))

        signal.connect(receiver, weak=False)
        receivers.append((signal, receiver))
    yield events
    for signal, receiver in receivers:
        signal.disconnect(receiver)


def _make_chain(root: Path, *names: str) -> None:
    """Create modules where each name depends on the one before it."""
    previous: str | None = None
    for name in names:
        manifest: dict[str, Any] = {"title": name, "version": "1"}
        if previous is not None:
            manifest["depends_on"] = [previous]
        make_fake_module(root, name, manifest=manifest, bootstrap=_BOOTSTRAP)
        previous = name


def _app_in_state(import_path: str, state: ModuleLifecycleState) -> AppConfig:
    """Build a standalone module config and move it to ``state`` through valid transitions only."""
    app = build_app_config(import_path)
    path = [ModuleLifecycleState.LOADED, ModuleLifecycleState.READY, ModuleLifecycleState.SHUTDOWN]
    if state is ModuleLifecycleState.SHUTDOWN:
        path = [ModuleLifecycleState.LOADED, ModuleLifecycleState.SHUTDOWN]
    for step in path:
        if app.lifecycle_state is state:
            break
        app.transition_to(step)
    return app


class TestFailureAtomicInstall:
    def test_direct_load_failure_leaves_module_uninstalled(
        self, fake_package: Path, fail_hooks: set[tuple[str, str]], signal_log: list[tuple[str, str | None]]
    ) -> None:
        _make_chain(fake_package, "solo")
        fail_hooks.add(("on_load", "solo"))
        registry = ModuleRegistry()

        with pytest.raises(ModuleLifecycleError, match="on_load.*raised an error"):
            registry.install("solo")

        assert registry.is_installed("solo") is False
        assert registry.list_installed() == []
        assert ("loaded", "solo") not in signal_log

    def test_install_can_be_retried_after_failure(
        self, fake_package: Path, fail_hooks: set[tuple[str, str]], lifecycle_log: list[str]
    ) -> None:
        _make_chain(fake_package, "base", "app")
        fail_hooks.add(("on_load", "app"))
        registry = ModuleRegistry()

        with pytest.raises(ModuleLifecycleError):
            registry.install("app")
        fail_hooks.clear()
        lifecycle_log.clear()
        registry.install("app")

        assert registry.list_installed() == ["base", "app"]
        assert lifecycle_log == ["on_load:base", "on_load:app"]

    def test_deep_dependency_failure_rolls_back_only_introduced_modules(
        self, fake_package: Path, fail_hooks: set[tuple[str, str]], lifecycle_log: list[str]
    ) -> None:
        make_fake_module(fake_package, "base", manifest={"title": "base", "version": "1"}, bootstrap=_BOOTSTRAP)
        make_fake_module(fake_package, "leaf", manifest={"title": "leaf", "version": "1"}, bootstrap=_BOOTSTRAP)
        make_fake_module(
            fake_package,
            "mid",
            manifest={"title": "mid", "version": "1", "depends_on": ["base", "leaf"]},
            bootstrap=_BOOTSTRAP,
        )
        make_fake_module(
            fake_package,
            "top",
            manifest={"title": "top", "version": "1", "depends_on": ["mid"]},
            bootstrap=_BOOTSTRAP,
        )
        registry = ModuleRegistry()
        registry.install("base")
        lifecycle_log.clear()
        fail_hooks.add(("on_load", "top"))

        with pytest.raises(ModuleLifecycleError, match="on_load.*raised an error"):
            registry.install("top")

        assert registry.list_installed() == ["base"]
        assert registry.is_installed("base") is True
        assert [name for name in ("leaf", "mid", "top") if registry.is_installed(name)] == []
        assert lifecycle_log == ["on_load:leaf", "on_load:mid", "on_load:top", "on_shutdown:mid", "on_shutdown:leaf"]

    def test_unwind_emits_module_shutdown_for_modules_that_had_loaded(
        self, fake_package: Path, fail_hooks: set[tuple[str, str]], signal_log: list[tuple[str, str | None]]
    ) -> None:
        _make_chain(fake_package, "one", "two", "three")
        fail_hooks.add(("on_load", "three"))
        registry = ModuleRegistry()

        with pytest.raises(ModuleLifecycleError):
            registry.install("three")

        assert signal_log == [
            ("loaded", "one"),
            ("loaded", "two"),
            ("shutdown", "two"),
            ("shutdown", "one"),
        ]

    def test_failed_install_keeps_modules_from_earlier_operations_usable(
        self, fake_package: Path, fail_hooks: set[tuple[str, str]], lifecycle_log: list[str]
    ) -> None:
        _make_chain(fake_package, "stable")
        _make_chain(fake_package, "broken")
        fail_hooks.add(("on_load", "broken"))
        registry = ModuleRegistry()
        registry.install("stable")

        with pytest.raises(ModuleLifecycleError):
            registry.install("broken")
        registry.mark_ready()
        registry.shutdown()

        assert "on_shutdown:stable" in lifecycle_log
        assert lifecycle_log.count("on_shutdown:stable") == 1

    def test_unwind_failure_keeps_both_causes(
        self, fake_package: Path, fail_hooks: set[tuple[str, str]], lifecycle_log: list[str]
    ) -> None:
        _make_chain(fake_package, "one", "two", "three")
        fail_hooks.update({("on_load", "three"), ("on_shutdown", "two")})
        registry = ModuleRegistry()

        with pytest.raises(ModuleStartupRollbackError) as exc_info:
            registry.install("three")

        error = exc_info.value
        assert isinstance(error, ModuleLifecycleError)
        assert [(f.module, f.phase) for f in error.failures] == [
            ("three", "on_load"),
            ("two", "on_shutdown"),
        ]
        assert str(error.failures[0].cause) == "on_load boom in three"
        assert str(error.failures[1].cause) == "on_shutdown boom in two"
        assert "on_load boom in three" in str(error)
        assert "on_shutdown boom in two" in str(error)
        assert isinstance(error.__cause__, ExceptionGroup)
        assert list(error.__cause__.exceptions) == [f.error for f in error.failures]
        assert registry.list_installed() == []
        assert lifecycle_log[-2:] == ["on_shutdown:two", "on_shutdown:one"]

    def test_non_hook_failure_is_also_rolled_back(self, fake_package: Path) -> None:
        make_fake_module(fake_package, "good", manifest={"title": "good", "version": "1"})
        make_fake_module(
            fake_package, "needs_missing", manifest={"title": "x", "version": "1", "depends_on": ["good", "missing"]}
        )
        registry = ModuleRegistry()

        with pytest.raises(Exception, match="missing"):
            registry.install("needs_missing")

        assert registry.list_installed() == []

    def test_populate_rolls_back_every_module_it_introduced(
        self, fake_package: Path, fail_hooks: set[tuple[str, str]]
    ) -> None:
        _make_chain(fake_package, "alpha")
        _make_chain(fake_package, "beta")
        fail_hooks.add(("on_load", "beta"))
        registry = ModuleRegistry()

        with pytest.raises(ModuleLifecycleError):
            registry.populate(["alpha", "beta"])

        assert registry.list_installed() == []
        assert registry.ready is False

        fail_hooks.clear()
        assert [app.name for app in registry.populate(["alpha", "beta"])] == ["alpha", "beta"]


class TestRegistryNotificationRetry:
    @pytest.mark.parametrize("operation", ["mark_ready", "shutdown"])
    def test_failed_notification_is_retried_without_repeating_hooks(
        self, fake_package: Path, lifecycle_log: list[str], operation: str
    ) -> None:
        _make_chain(fake_package, "notify")
        registry = ModuleRegistry()
        registry.install("notify")
        lifecycle_log.clear()
        signal = registry_ready if operation == "mark_ready" else registry_shutdown
        attempts: list[str] = []
        completed: list[str] = []

        def receiver(sender: Any, **kwargs: Any) -> None:
            attempts.append(operation)
            if operation == "mark_ready":
                assert [app.name for app in registry.all()] == ["notify"]
            if len(attempts) == 1:
                raise RuntimeError("temporary notification failure")
            completed.append(operation)

        signal.connect(receiver, weak=False)
        try:
            with pytest.raises(RuntimeError):
                getattr(registry, operation)()
            if operation == "mark_ready":
                assert registry.ready is False
            getattr(registry, operation)()
            getattr(registry, operation)()
        finally:
            signal.disconnect(receiver)

        assert attempts == [operation, operation]
        assert completed == [operation]
        hook = "ready" if operation == "mark_ready" else "on_shutdown"
        assert lifecycle_log == [f"{hook}:notify"]

    def test_notification_retry_after_adding_a_module(self, fake_package: Path, lifecycle_log: list[str]) -> None:
        _make_chain(fake_package, "first", "second")
        registry = ModuleRegistry()
        registry.populate(["first"])
        registry.install("second")
        lifecycle_log.clear()
        completed: list[str] = []
        fail = True

        def receiver(sender: Any, **kwargs: Any) -> None:
            if fail:
                raise RuntimeError("temporary notification failure")
            completed.extend(app.name for app in registry.all())

        registry_ready.connect(receiver, weak=False)
        try:
            with pytest.raises(RuntimeError):
                registry.mark_ready()
            fail = False
            registry.mark_ready()
            registry.mark_ready()
        finally:
            registry_ready.disconnect(receiver)

        assert completed == ["first", "second"]
        assert lifecycle_log == ["ready:second"]


class TestRollbackInstallationBoundary:
    @pytest.mark.parametrize("entrypoint", ["install", "populate"])
    @pytest.mark.parametrize("callback", ["hook", "signal"])
    def test_rollback_rejects_new_modules_and_releases_guard(
        self,
        fake_package: Path,
        fail_hooks: set[tuple[str, str]],
        lifecycle_log: list[str],
        entrypoint: str,
        callback: str,
    ) -> None:
        _make_chain(fake_package, "existing")
        _make_chain(fake_package, "dependency", "broken")
        _make_chain(fake_package, "late")
        registry = ModuleRegistry()
        registry.install("existing")
        fail_hooks.add(("on_load", "broken"))

        def install_during_cleanup(*args: Any, **kwargs: Any) -> None:
            if entrypoint == "install":
                registry.install("late")
            else:
                registry.populate(["late"])

        if callback == "hook":
            import importlib

            bootstrap = importlib.import_module("dependency.bootstrap")
            bootstrap.on_shutdown = install_during_cleanup
        else:
            module_shutdown.connect(install_during_cleanup, weak=False)
        try:
            with pytest.raises(ModuleStartupRollbackError) as exc_info:
                registry.install("broken")
        finally:
            if callback == "signal":
                module_shutdown.disconnect(install_during_cleanup)

        assert registry.list_installed() == ["existing"]
        assert "on_load:late" not in lifecycle_log
        assert exc_info.value.failed_modules == ["broken", "dependency"]
        assert isinstance(exc_info.value.failures[0].cause, RuntimeError)
        assert isinstance(exc_info.value.failures[1].cause, ModuleLifecycleError)
        registry.install("late")
        assert registry.list_installed() == ["existing", "late"]


class TestRetrySafeReadiness:
    def test_retry_skips_modules_that_are_already_ready(
        self,
        fake_package: Path,
        fail_hooks: set[tuple[str, str]],
        lifecycle_log: list[str],
        signal_log: list[tuple[str, str | None]],
    ) -> None:
        _make_chain(fake_package, "a", "b", "c")
        registry = ModuleRegistry()
        registry.install("c")
        lifecycle_log.clear()
        fail_hooks.add(("ready", "b"))

        with pytest.raises(ModuleLifecycleError, match="ready.*raised an error"):
            registry.mark_ready()

        assert registry.ready is False
        assert ("registry_ready", None) not in signal_log
        assert [event for event in signal_log if event[0] == "ready"] == [("ready", "a")]

        fail_hooks.clear()
        registry.mark_ready()

        assert lifecycle_log == ["ready:a", "ready:b", "ready:b", "ready:c"]
        assert registry.ready is True
        assert [event for event in signal_log if event[0] == "ready"] == [
            ("ready", "a"),
            ("ready", "b"),
            ("ready", "c"),
        ]
        assert signal_log.count(("registry_ready", None)) == 1

    def test_populate_can_resume_after_ready_failure(
        self, fake_package: Path, fail_hooks: set[tuple[str, str]], lifecycle_log: list[str]
    ) -> None:
        _make_chain(fake_package, "a", "b")
        fail_hooks.add(("ready", "b"))
        registry = ModuleRegistry()

        with pytest.raises(ModuleLifecycleError):
            registry.populate(["b"])
        assert registry.list_installed() == ["a", "b"]

        fail_hooks.clear()
        registry.populate(["b"])

        assert lifecycle_log.count("ready:a") == 1
        assert lifecycle_log.count("on_load:a") == 1
        assert registry.ready is True

    def test_repeated_mark_ready_is_harmless(
        self, fake_package: Path, lifecycle_log: list[str], signal_log: list[tuple[str, str | None]]
    ) -> None:
        _make_chain(fake_package, "only")
        registry = ModuleRegistry()
        registry.install("only")

        registry.mark_ready()
        registry.mark_ready()

        assert lifecycle_log.count("ready:only") == 1
        assert signal_log.count(("registry_ready", None)) == 1

    def test_mark_ready_after_shutdown_is_rejected(self, fake_package: Path) -> None:
        _make_chain(fake_package, "gone")
        registry = ModuleRegistry()
        registry.populate(["gone"])
        registry.shutdown()

        with pytest.raises(ModuleLifecycleError, match="already shut down: gone"):
            registry.mark_ready()


class TestResilientShutdown:
    def test_every_module_is_attempted_and_all_failures_are_reported(
        self,
        fake_package: Path,
        fail_hooks: set[tuple[str, str]],
        lifecycle_log: list[str],
        signal_log: list[tuple[str, str | None]],
    ) -> None:
        _make_chain(fake_package, "a", "b", "c", "d")
        registry = ModuleRegistry()
        registry.populate(["d"])
        lifecycle_log.clear()
        signal_log.clear()
        fail_hooks.update({("on_shutdown", "d"), ("on_shutdown", "b")})

        with pytest.raises(ModuleLifecycleError) as exc_info:
            registry.shutdown()

        error = exc_info.value
        assert lifecycle_log == ["on_shutdown:d", "on_shutdown:c", "on_shutdown:b", "on_shutdown:a"]
        assert error.failed_modules == ["d", "b"]
        assert [str(f.cause) for f in error.failures] == ["on_shutdown boom in d", "on_shutdown boom in b"]
        assert "on_shutdown boom in d" in str(error)
        assert "on_shutdown boom in b" in str(error)
        assert signal_log == [("shutdown", "c"), ("shutdown", "a")]

    def test_failed_modules_are_retried_and_completed_ones_are_not(
        self,
        fake_package: Path,
        fail_hooks: set[tuple[str, str]],
        lifecycle_log: list[str],
        signal_log: list[tuple[str, str | None]],
    ) -> None:
        _make_chain(fake_package, "a", "b")
        registry = ModuleRegistry()
        registry.populate(["b"])
        lifecycle_log.clear()
        fail_hooks.add(("on_shutdown", "b"))

        with pytest.raises(ModuleLifecycleError):
            registry.shutdown()
        fail_hooks.clear()
        registry.shutdown()

        assert lifecycle_log == ["on_shutdown:b", "on_shutdown:a", "on_shutdown:b"]
        assert signal_log.count(("registry_shutdown", None)) == 1

    def test_registry_shutdown_signal_only_after_every_module_shut_down(
        self, fake_package: Path, fail_hooks: set[tuple[str, str]], signal_log: list[tuple[str, str | None]]
    ) -> None:
        _make_chain(fake_package, "a")
        registry = ModuleRegistry()
        registry.populate(["a"])
        fail_hooks.add(("on_shutdown", "a"))

        with pytest.raises(ModuleLifecycleError):
            registry.shutdown()

        assert ("registry_shutdown", None) not in signal_log

    def test_failing_signal_receiver_does_not_stop_remaining_modules(
        self, fake_package: Path, lifecycle_log: list[str]
    ) -> None:
        _make_chain(fake_package, "a", "b")
        registry = ModuleRegistry()
        registry.populate(["b"])
        lifecycle_log.clear()

        def broken_receiver(sender: Any, **kwargs: Any) -> None:
            raise RuntimeError("receiver boom")

        module_shutdown.connect(broken_receiver, weak=False)
        try:
            with pytest.raises(ModuleLifecycleError, match="receiver boom") as exc_info:
                registry.shutdown()
        finally:
            module_shutdown.disconnect(broken_receiver)

        assert lifecycle_log == ["on_shutdown:b", "on_shutdown:a"]
        assert exc_info.value.failed_modules == ["b", "a"]

    def test_repeated_shutdown_never_calls_a_hook_twice(
        self, fake_package: Path, lifecycle_log: list[str], signal_log: list[tuple[str, str | None]]
    ) -> None:
        _make_chain(fake_package, "a", "b")
        registry = ModuleRegistry()
        registry.populate(["b"])
        lifecycle_log.clear()

        registry.shutdown()
        registry.shutdown()

        assert lifecycle_log == ["on_shutdown:b", "on_shutdown:a"]
        assert [event for event in signal_log if event[0] == "shutdown"] == [("shutdown", "b"), ("shutdown", "a")]
        assert signal_log.count(("registry_shutdown", None)) == 1

    def test_modules_that_never_became_ready_are_still_shut_down(
        self, fake_package: Path, fail_hooks: set[tuple[str, str]], lifecycle_log: list[str]
    ) -> None:
        _make_chain(fake_package, "a", "b")
        registry = ModuleRegistry()
        registry.install("b")
        fail_hooks.add(("ready", "a"))
        with pytest.raises(ModuleLifecycleError):
            registry.mark_ready()
        lifecycle_log.clear()

        registry.shutdown()

        assert lifecycle_log == ["on_shutdown:b", "on_shutdown:a"]


class TestLifecycleStates:
    def test_states_advance_through_the_public_lifecycle(self, fake_package: Path) -> None:
        _make_chain(fake_package, "stateful")
        registry = ModuleRegistry()

        app = registry.install("stateful")
        assert app.lifecycle_state is ModuleLifecycleState.LOADED
        registry.mark_ready()
        assert app.lifecycle_state is ModuleLifecycleState.READY
        registry.shutdown()
        assert app.lifecycle_state is ModuleLifecycleState.SHUTDOWN

    def test_state_compares_equal_to_its_string_value(self, fake_package: Path) -> None:
        _make_chain(fake_package, "strval")
        app = ModuleRegistry().install("strval")

        assert app.lifecycle_state == "loaded"

    @pytest.mark.parametrize(
        ("start", "target"),
        [
            (ModuleLifecycleState.LOADED, ModuleLifecycleState.LOADING),
            (ModuleLifecycleState.READY, ModuleLifecycleState.LOADED),
            (ModuleLifecycleState.SHUTDOWN, ModuleLifecycleState.READY),
            (ModuleLifecycleState.LOADING, ModuleLifecycleState.READY),
            (ModuleLifecycleState.READY, ModuleLifecycleState.READY),
        ],
    )
    def test_invalid_transitions_are_rejected(
        self, fake_package: Path, start: ModuleLifecycleState, target: Any
    ) -> None:
        _make_chain(fake_package, "transitions")
        app = _app_in_state("transitions", start)

        with pytest.raises(ModuleLifecycleError, match="cannot move from lifecycle state"):
            app.transition_to(target)

        assert app.lifecycle_state is start

    def test_transition_to_accepts_a_state_value_string(self, fake_package: Path) -> None:
        _make_chain(fake_package, "strtransition")
        app = _app_in_state("strtransition", ModuleLifecycleState.LOADING)

        app.transition_to("loaded")  # type: ignore[arg-type]

        assert app.lifecycle_state is ModuleLifecycleState.LOADED

    def test_transition_to_rejects_an_unknown_state_name(self, fake_package: Path) -> None:
        _make_chain(fake_package, "unknown_state")
        app = _app_in_state("unknown_state", ModuleLifecycleState.LOADING)

        with pytest.raises(ValueError, match="bogus"):
            app.transition_to("bogus")  # type: ignore[arg-type]

        assert app.lifecycle_state is ModuleLifecycleState.LOADING


class TestLifecycleStateIsReadOnly:
    @pytest.mark.parametrize("value", [ModuleLifecycleState.SHUTDOWN, "shutdown", "bogus"])
    def test_direct_assignment_is_rejected_and_state_is_unchanged(self, fake_package: Path, value: Any) -> None:
        _make_chain(fake_package, "guarded")
        app = _app_in_state("guarded", ModuleLifecycleState.LOADED)

        with pytest.raises(AttributeError):
            app.lifecycle_state = value

        assert app.lifecycle_state is ModuleLifecycleState.LOADED

    def test_assignment_cannot_make_shutdown_skip_a_module(self, fake_package: Path, lifecycle_log: list[str]) -> None:
        _make_chain(fake_package, "a", "b")
        registry = ModuleRegistry()
        app_b = registry.install("b")
        lifecycle_log.clear()

        with pytest.raises(AttributeError):
            app_b.lifecycle_state = ModuleLifecycleState.SHUTDOWN  # type: ignore[misc]
        registry.shutdown()

        assert lifecycle_log == ["on_shutdown:b", "on_shutdown:a"]

    def test_state_is_rejected_as_a_constructor_argument(self, fake_package: Path) -> None:
        _make_chain(fake_package, "ctor")
        template = build_app_config("ctor")
        fields = {name: getattr(template, name) for name in AppConfig.model_fields}

        with pytest.raises(ValidationError, match="lifecycle_state"):
            AppConfig(**fields, lifecycle_state=ModuleLifecycleState.READY)
        with pytest.raises(ValidationError, match="lifecycle_state"):
            AppConfig.model_validate({**fields, "lifecycle_state": "ready"})

        assert template.lifecycle_state is ModuleLifecycleState.LOADING

    def test_state_still_appears_in_the_repr_and_dump(self, fake_package: Path) -> None:
        _make_chain(fake_package, "shown")
        app = build_app_config("shown")

        assert "lifecycle_state=" in repr(app)
        assert app.model_dump()["lifecycle_state"] is ModuleLifecycleState.LOADING

    def test_a_copy_keeps_the_state_it_was_copied_in(self, fake_package: Path) -> None:
        _make_chain(fake_package, "copied")
        app = _app_in_state("copied", ModuleLifecycleState.READY)

        assert app.model_copy().lifecycle_state is ModuleLifecycleState.READY


class TestAppConfigKeepsItsOtherModelSemantics:
    """Making the state read-only must not change how every other ``AppConfig`` field behaves."""

    def test_unrelated_unknown_keyword_arguments_are_still_ignored(self, fake_package: Path) -> None:
        _make_chain(fake_package, "extras")
        template = build_app_config("extras")
        fields = {name: getattr(template, name) for name in AppConfig.model_fields}

        app = AppConfig(**fields, not_a_field="ignored")

        assert not hasattr(app, "not_a_field")
        assert app.lifecycle_state is ModuleLifecycleState.LOADING

    def test_assignment_to_other_fields_is_still_validated_and_coerced(self, fake_package: Path) -> None:
        _make_chain(fake_package, "validated")
        app = build_app_config("validated")

        app.package_dir = str(fake_package)  # type: ignore[assignment]
        assert app.package_dir == fake_package
        assert isinstance(app.package_dir, Path)

        with pytest.raises(ValidationError, match="name"):
            app.name = 5  # type: ignore[assignment]
        with pytest.raises(ValidationError, match="bootstrap_module"):
            app.bootstrap_module = 1  # type: ignore[assignment]
        assert app.name == "validated"

    def test_assigning_an_undeclared_attribute_still_raises_a_validation_error(self, fake_package: Path) -> None:
        _make_chain(fake_package, "undeclared")
        app = build_app_config("undeclared")

        with pytest.raises(ValidationError, match="not_a_field"):
            app.not_a_field = 1  # type: ignore[attr-defined]


class TestFailureAttribution:
    def test_failure_in_later_populate_path_is_attributed_to_that_path(
        self, fake_package: Path, fail_hooks: set[tuple[str, str]]
    ) -> None:
        _make_chain(fake_package, "alpha")
        _make_chain(fake_package, "beta")
        fail_hooks.update({("on_load", "beta"), ("on_shutdown", "alpha")})
        registry = ModuleRegistry()

        with pytest.raises(ModuleStartupRollbackError) as exc_info:
            registry.populate(["alpha", "beta"])

        assert [(f.module, f.phase) for f in exc_info.value.failures] == [
            ("beta", "on_load"),
            ("alpha", "on_shutdown"),
        ]

    def test_manifest_failure_is_attributed_to_the_broken_dependency(
        self, fake_package: Path, fail_hooks: set[tuple[str, str]]
    ) -> None:
        _make_chain(fake_package, "healthy")
        make_fake_module(
            fake_package,
            "parent",
            manifest={"title": "parent", "version": "1", "depends_on": ["healthy", "no_such_module"]},
        )
        fail_hooks.add(("on_shutdown", "healthy"))
        registry = ModuleRegistry()

        with pytest.raises(ModuleStartupRollbackError) as exc_info:
            registry.install("parent")

        failures = exc_info.value.failures
        assert (failures[0].module, failures[0].phase) == ("no_such_module", "manifest")
        assert (failures[1].module, failures[1].phase) == ("healthy", "on_shutdown")

    def test_dependency_cycle_is_attributed_with_dependency_phase(
        self, fake_package: Path, fail_hooks: set[tuple[str, str]]
    ) -> None:
        _make_chain(fake_package, "base")
        make_fake_module(
            fake_package, "ring_a", manifest={"title": "a", "version": "1", "depends_on": ["base", "ring_b"]}
        )
        make_fake_module(fake_package, "ring_b", manifest={"title": "b", "version": "1", "depends_on": ["ring_a"]})
        fail_hooks.add(("on_shutdown", "base"))
        registry = ModuleRegistry()

        with pytest.raises(ModuleStartupRollbackError) as exc_info:
            registry.install("ring_a")

        first = exc_info.value.failures[0]
        assert (first.module, first.phase) == ("ring_a", "dependency")

    def test_module_loaded_receiver_failure_is_attributed_and_the_module_unwound(
        self, fake_package: Path, fail_hooks: set[tuple[str, str]], lifecycle_log: list[str]
    ) -> None:
        _make_chain(fake_package, "watched")
        fail_hooks.add(("on_shutdown", "watched"))
        registry = ModuleRegistry()

        def broken_receiver(sender: Any, **kwargs: Any) -> None:
            if kwargs["registry"] is registry:
                raise RuntimeError("receiver boom")

        module_loaded.connect(broken_receiver, weak=False)
        try:
            with pytest.raises(ModuleStartupRollbackError) as exc_info:
                registry.install("watched")
        finally:
            module_loaded.disconnect(broken_receiver)

        failures = exc_info.value.failures
        assert [(f.module, f.phase) for f in failures] == [("watched", "module_loaded"), ("watched", "on_shutdown")]
        assert str(failures[0].cause) == "receiver boom"
        assert lifecycle_log == ["on_load:watched", "on_shutdown:watched"]
        assert registry.list_installed() == []


class TestReadyFailureReporting:
    def test_ready_failure_exposes_failures_like_shutdown(
        self, fake_package: Path, fail_hooks: set[tuple[str, str]]
    ) -> None:
        _make_chain(fake_package, "a", "b")
        fail_hooks.add(("ready", "b"))
        registry = ModuleRegistry()
        registry.install("b")

        with pytest.raises(ModuleLifecycleError, match="ready.*raised an error") as exc_info:
            registry.mark_ready()

        error = exc_info.value
        assert error.failed_modules == ["b"]
        assert [(f.module, f.phase) for f in error.failures] == [("b", "ready")]
        assert str(error.failures[0].cause) == "ready boom in b"
        assert isinstance(error.__cause__, ExceptionGroup)


class TestReentrancy:
    def test_ready_hook_calling_mark_ready_is_rejected_without_recursion(
        self, fake_package: Path, reenter_hooks: dict[tuple[str, str], str], lifecycle_log: list[str]
    ) -> None:
        _make_chain(fake_package, "a", "b")
        reenter_hooks[("ready", "a")] = "mark_ready"
        registry = ModuleRegistry()
        registry.install("b")

        with pytest.raises(
            ModuleLifecycleError, match="Cannot call mark_ready\\(\\) while mark_ready\\(\\) is running"
        ):
            registry.mark_ready()

        assert lifecycle_log == ["on_load:a", "on_load:b", "ready:a"]
        assert registry.ready is False

        reenter_hooks.clear()
        registry.mark_ready()

        assert lifecycle_log.count("ready:a") == 2
        assert lifecycle_log.count("ready:b") == 1
        assert registry.ready is True

    def test_ready_hook_calling_shutdown_is_rejected_without_calling_hooks(
        self, fake_package: Path, reenter_hooks: dict[tuple[str, str], str], lifecycle_log: list[str]
    ) -> None:
        _make_chain(fake_package, "a")
        reenter_hooks[("ready", "a")] = "shutdown"
        registry = ModuleRegistry()
        registry.install("a")

        with pytest.raises(ModuleLifecycleError, match="Cannot call shutdown\\(\\) while mark_ready\\(\\) is running"):
            registry.mark_ready()

        assert "on_shutdown:a" not in lifecycle_log

    def test_shutdown_hook_calling_shutdown_does_not_recurse_and_others_still_run(
        self, fake_package: Path, reenter_hooks: dict[tuple[str, str], str], lifecycle_log: list[str]
    ) -> None:
        _make_chain(fake_package, "a", "b", "c")
        registry = ModuleRegistry()
        registry.populate(["c"])
        lifecycle_log.clear()
        reenter_hooks[("on_shutdown", "b")] = "shutdown"

        with pytest.raises(
            ModuleLifecycleError, match="Cannot call shutdown\\(\\) while shutdown\\(\\) is running"
        ) as exc_info:
            registry.shutdown()

        assert lifecycle_log == ["on_shutdown:c", "on_shutdown:b", "on_shutdown:a"]
        assert exc_info.value.failed_modules == ["b"]

        reenter_hooks.clear()
        registry.shutdown()

        assert lifecycle_log == ["on_shutdown:c", "on_shutdown:b", "on_shutdown:a", "on_shutdown:b"]

    def test_shutdown_hook_calling_mark_ready_is_rejected(
        self, fake_package: Path, reenter_hooks: dict[tuple[str, str], str], lifecycle_log: list[str]
    ) -> None:
        _make_chain(fake_package, "a")
        registry = ModuleRegistry()
        registry.populate(["a"])
        lifecycle_log.clear()
        reenter_hooks[("on_shutdown", "a")] = "mark_ready"

        with pytest.raises(ModuleLifecycleError, match="Cannot call mark_ready\\(\\) while shutdown\\(\\) is running"):
            registry.shutdown()

        assert lifecycle_log == ["on_shutdown:a"]

    def test_signal_receiver_calling_back_into_the_registry_is_rejected(
        self, fake_package: Path, lifecycle_log: list[str]
    ) -> None:
        _make_chain(fake_package, "a", "b")
        registry = ModuleRegistry()
        registry.install("b")
        lifecycle_log.clear()

        def receiver(sender: Any, **kwargs: Any) -> None:
            if kwargs["registry"] is registry:
                registry.shutdown()

        module_ready.connect(receiver, weak=False)
        try:
            with pytest.raises(
                ModuleLifecycleError, match="Cannot call shutdown\\(\\) while mark_ready\\(\\) is running"
            ):
                registry.mark_ready()
        finally:
            module_ready.disconnect(receiver)

        assert lifecycle_log == ["ready:a"]

    def test_guard_is_released_after_a_failed_operation(
        self, fake_package: Path, fail_hooks: set[tuple[str, str]], lifecycle_log: list[str]
    ) -> None:
        _make_chain(fake_package, "a")
        fail_hooks.add(("ready", "a"))
        registry = ModuleRegistry()
        registry.install("a")

        with pytest.raises(ModuleLifecycleError):
            registry.mark_ready()
        fail_hooks.clear()
        registry.mark_ready()
        registry.shutdown()

        assert lifecycle_log.count("on_shutdown:a") == 1

    def test_nested_install_from_on_load_hook_is_still_supported(self, fake_package: Path) -> None:
        make_fake_module(fake_package, "inner", manifest={"title": "inner", "version": "1"})
        make_fake_module(
            fake_package,
            "outer",
            manifest={"title": "outer", "version": "1"},
            bootstrap="def on_load(*, registry): registry.install('inner')\n",
        )
        registry = ModuleRegistry()

        registry.install("outer")

        assert registry.list_installed() == ["outer", "inner"]


class TestInstallRejectsLifecycleCallbacks:
    """``shutdown()`` and ``mark_ready()`` are rejected while an installation is running."""

    @pytest.mark.parametrize("method", ["shutdown", "mark_ready"])
    def test_on_load_hook_cannot_call_back_and_the_install_rolls_back_cleanly(
        self,
        fake_package: Path,
        reenter_hooks: dict[tuple[str, str], str],
        fail_hooks: set[tuple[str, str]],
        lifecycle_log: list[str],
        method: str,
    ) -> None:
        _make_chain(fake_package, "a", "b", "c")
        reenter_hooks[("on_load", "c")] = method
        fail_hooks.add(("on_shutdown", "b"))
        registry = ModuleRegistry()

        with pytest.raises(ModuleStartupRollbackError) as exc_info:
            registry.install("c")

        failures = exc_info.value.failures
        assert [(f.module, f.phase) for f in failures] == [("c", "on_load"), ("b", "on_shutdown")]
        assert f"Cannot call {method}() while install() is running" in str(failures[0].error)
        assert lifecycle_log == ["on_load:a", "on_load:b", "on_load:c", "on_shutdown:b", "on_shutdown:a"]
        assert registry.list_installed() == []

    def test_a_hook_that_swallows_the_rejection_keeps_the_install_intact(self, fake_package: Path) -> None:
        make_fake_module(
            fake_package,
            "tolerant",
            manifest={"title": "tolerant", "version": "1"},
            bootstrap=(
                "from bedrock.module import ModuleLifecycleError\n\n\n"
                "def on_load(*, registry):\n"
                "    try:\n"
                "        registry.shutdown()\n"
                "    except ModuleLifecycleError:\n"
                "        pass\n"
            ),
        )
        registry = ModuleRegistry()

        app = registry.install("tolerant")
        registry.mark_ready()

        assert app.lifecycle_state is ModuleLifecycleState.READY
        assert registry.list_installed() == ["tolerant"]

    def test_module_loaded_receiver_cannot_shut_down_the_registry(
        self, fake_package: Path, lifecycle_log: list[str]
    ) -> None:
        _make_chain(fake_package, "a", "b")
        registry = ModuleRegistry()

        def receiver(sender: Any, **kwargs: Any) -> None:
            if kwargs["registry"] is registry and kwargs["module"].name == "b":
                registry.shutdown()

        module_loaded.connect(receiver, weak=False)
        try:
            with pytest.raises(ModuleLifecycleError) as exc_info:
                registry.install("b")
        finally:
            module_loaded.disconnect(receiver)

        assert "Cannot call shutdown() while install() is running" in str(exc_info.value)
        assert lifecycle_log == ["on_load:a", "on_load:b", "on_shutdown:b", "on_shutdown:a"]
        assert registry.list_installed() == []

    def test_rollback_hook_cannot_call_shutdown_and_cleanup_still_attempts_every_module(
        self,
        fake_package: Path,
        reenter_hooks: dict[tuple[str, str], str],
        fail_hooks: set[tuple[str, str]],
        lifecycle_log: list[str],
    ) -> None:
        _make_chain(fake_package, "a", "b", "c")
        fail_hooks.add(("on_load", "c"))
        reenter_hooks[("on_shutdown", "b")] = "shutdown"
        registry = ModuleRegistry()

        with pytest.raises(ModuleStartupRollbackError) as exc_info:
            registry.install("c")

        assert [(f.module, f.phase) for f in exc_info.value.failures] == [("c", "on_load"), ("b", "on_shutdown")]
        assert lifecycle_log == ["on_load:a", "on_load:b", "on_load:c", "on_shutdown:b", "on_shutdown:a"]
        assert registry.list_installed() == []

    def test_shutdown_and_install_work_normally_after_a_rejected_callback(
        self, fake_package: Path, reenter_hooks: dict[tuple[str, str], str], lifecycle_log: list[str]
    ) -> None:
        _make_chain(fake_package, "a", "b")
        reenter_hooks[("on_load", "b")] = "shutdown"
        registry = ModuleRegistry()
        with pytest.raises(ModuleLifecycleError):
            registry.install("b")

        reenter_hooks.clear()
        app = registry.install("b")
        registry.populate(["b"])
        registry.shutdown()

        assert app.lifecycle_state is ModuleLifecycleState.SHUTDOWN
        assert lifecycle_log == [
            *["on_load:a", "on_load:b", "on_shutdown:a"],  # b never finished loading, so only a is unwound
            *["on_load:a", "on_load:b", "ready:a", "ready:b", "on_shutdown:b", "on_shutdown:a"],
        ]


_CATCHING_OUTER = """
import builtins

from bedrock.module import ModuleLifecycleError


def on_load(*, registry):
    builtins._lifecycle_calls.append("on_load:outer")
    try:
        registry.install("bad_inner")
    except ModuleLifecycleError:
        builtins._lifecycle_calls.append("nested_install_failed")
    builtins._lifecycle_calls.append(f"installed_after_nested_failure:{registry.list_installed()}")
    try:
        registry.shutdown()
    except ModuleLifecycleError as exc:
        builtins._lifecycle_calls.append(f"shutdown_rejected:{'install() is running' in str(exc)}")
        if PROPAGATE:
            raise
"""


class TestNestedInstallFailureKeepsTheOuterGuard:
    """A failed nested install is rolled back alone and leaves the outer installation guarded."""

    def _make_outer(self, fake_package: Path, *, propagate: bool) -> None:
        _make_chain(fake_package, "dep")
        _make_chain(fake_package, "bad_inner")
        make_fake_module(
            fake_package,
            "outer",
            manifest={"title": "outer", "version": "1", "depends_on": ["dep"]},
            bootstrap=_CATCHING_OUTER.replace("PROPAGATE", repr(propagate)),
        )

    def test_caught_nested_failure_keeps_the_outer_install_and_still_rejects_shutdown(
        self, fake_package: Path, fail_hooks: set[tuple[str, str]], lifecycle_log: list[str]
    ) -> None:
        self._make_outer(fake_package, propagate=False)
        fail_hooks.add(("on_load", "bad_inner"))
        registry = ModuleRegistry()

        outer = registry.install("outer")

        assert lifecycle_log == [
            "on_load:dep",
            "on_load:outer",
            "on_load:bad_inner",
            "nested_install_failed",
            "installed_after_nested_failure:['dep', 'outer']",
            "shutdown_rejected:True",
        ]
        assert registry.list_installed() == ["dep", "outer"]
        assert outer.lifecycle_state is ModuleLifecycleState.LOADED

        lifecycle_log.clear()
        registry.mark_ready()
        registry.shutdown()
        assert lifecycle_log == ["ready:dep", "on_shutdown:dep"]

    def test_the_guard_is_released_so_the_registry_is_usable_after_the_outer_install(
        self, fake_package: Path, fail_hooks: set[tuple[str, str]], lifecycle_log: list[str]
    ) -> None:
        self._make_outer(fake_package, propagate=False)
        _make_chain(fake_package, "later")
        fail_hooks.add(("on_load", "bad_inner"))
        registry = ModuleRegistry()
        registry.install("outer")

        registry.install("later")
        registry.mark_ready()
        registry.shutdown()

        assert registry.list_installed() == ["dep", "outer", "later"]
        assert all(app.lifecycle_state is ModuleLifecycleState.SHUTDOWN for app in registry.all())

    def test_propagated_error_rolls_back_the_outer_install_and_attempts_every_cleanup(
        self, fake_package: Path, fail_hooks: set[tuple[str, str]], lifecycle_log: list[str]
    ) -> None:
        self._make_outer(fake_package, propagate=True)
        fail_hooks.update({("on_load", "bad_inner"), ("on_shutdown", "dep")})
        registry = ModuleRegistry()

        with pytest.raises(ModuleStartupRollbackError) as exc_info:
            registry.install("outer")

        assert [(f.module, f.phase) for f in exc_info.value.failures] == [("outer", "on_load"), ("dep", "on_shutdown")]
        assert "shutdown_rejected:True" in lifecycle_log
        assert lifecycle_log[-1] == "on_shutdown:dep"
        assert registry.list_installed() == []

        fail_hooks.clear()
        lifecycle_log.clear()
        registry.install("dep")
        registry.mark_ready()
        registry.shutdown()
        assert lifecycle_log == ["on_load:dep", "ready:dep", "on_shutdown:dep"]


class TestInstallDuringReadinessOrShutdown:
    """A module cannot be introduced while ``mark_ready()`` or ``shutdown()`` is running."""

    def test_ready_hook_cannot_install_and_no_partial_state_is_left(
        self, fake_package: Path, reenter_hooks: dict[tuple[str, str], str], lifecycle_log: list[str]
    ) -> None:
        _make_chain(fake_package, "a")
        _make_chain(fake_package, "late")
        reenter_hooks[("ready", "a")] = "install:late"
        registry = ModuleRegistry()
        registry.install("a")

        with pytest.raises(
            ModuleLifecycleError, match="Cannot call install\\(\\) while mark_ready\\(\\) is running"
        ) as exc_info:
            registry.mark_ready()

        assert exc_info.value.failed_modules == ["a"]
        assert registry.list_installed() == ["a"]
        assert registry.is_installed("late") is False
        assert "on_load:late" not in lifecycle_log
        assert registry.ready is False

    def test_ready_failure_from_rejected_install_is_recoverable_in_the_supported_order(
        self, fake_package: Path, reenter_hooks: dict[tuple[str, str], str], lifecycle_log: list[str]
    ) -> None:
        _make_chain(fake_package, "a")
        _make_chain(fake_package, "late")
        reenter_hooks[("ready", "a")] = "install:late"
        registry = ModuleRegistry()
        registry.install("a")
        with pytest.raises(ModuleLifecycleError):
            registry.mark_ready()

        reenter_hooks.clear()
        late = registry.install("late")
        registry.mark_ready()

        assert registry.ready is True
        assert [app.name for app in registry.all()] == ["a", "late"]
        assert late.lifecycle_state is ModuleLifecycleState.READY

    def test_installing_after_the_registry_is_ready_is_still_supported(self, fake_package: Path) -> None:
        _make_chain(fake_package, "a")
        _make_chain(fake_package, "late")
        registry = ModuleRegistry()
        registry.populate(["a"])

        late = registry.install("late")

        # ``registry.ready`` means readiness was announced, not that every installed module is ready.
        assert registry.ready is True
        assert late.lifecycle_state is ModuleLifecycleState.LOADED

        registry.mark_ready()

        assert registry.ready is True
        assert late.lifecycle_state is ModuleLifecycleState.READY

    def test_installing_an_already_installed_module_from_a_ready_hook_is_a_no_op(
        self, fake_package: Path, reenter_hooks: dict[tuple[str, str], str], lifecycle_log: list[str]
    ) -> None:
        _make_chain(fake_package, "a", "b")
        reenter_hooks[("ready", "b")] = "install:a"
        registry = ModuleRegistry()

        registry.populate(["b"])

        assert registry.ready is True
        assert lifecycle_log.count("on_load:a") == 1

    def test_on_shutdown_hook_cannot_install_and_other_modules_still_shut_down(
        self,
        fake_package: Path,
        reenter_hooks: dict[tuple[str, str], str],
        lifecycle_log: list[str],
    ) -> None:
        _make_chain(fake_package, "a", "b")
        _make_chain(fake_package, "late")
        registry = ModuleRegistry()
        registry.populate(["b"])
        lifecycle_log.clear()
        reenter_hooks[("on_shutdown", "b")] = "install:late"

        with pytest.raises(
            ModuleLifecycleError, match="Cannot call install\\(\\) while shutdown\\(\\) is running"
        ) as exc_info:
            registry.shutdown()

        assert exc_info.value.failed_modules == ["b"]
        assert lifecycle_log == ["on_shutdown:b", "on_shutdown:a"]
        assert registry.list_installed() == ["a", "b"]

    @pytest.mark.parametrize(
        ("signal", "operation"),
        [
            (module_ready, "mark_ready"),
            (registry_ready, "mark_ready"),
            (module_shutdown, "shutdown"),
            (registry_shutdown, "shutdown"),
        ],
    )
    def test_signal_receiver_cannot_install_during_the_operation(
        self, fake_package: Path, lifecycle_log: list[str], signal: Any, operation: str
    ) -> None:
        _make_chain(fake_package, "a")
        _make_chain(fake_package, "late")
        registry = ModuleRegistry()
        registry.install("a")

        def receiver(sender: Any, **kwargs: Any) -> None:
            if kwargs["registry"] is registry:
                registry.install("late")

        signal.connect(receiver, weak=False)
        try:
            if operation == "shutdown":
                registry.mark_ready()
            with pytest.raises(ModuleLifecycleError):
                getattr(registry, operation)()
        finally:
            signal.disconnect(receiver)

        assert registry.list_installed() == ["a"]
        assert "on_load:late" not in lifecycle_log


class TestPopulateHoldsLock:
    """Other threads wait for a whole ``populate()``; coordination uses events, never sleeps."""

    _WAIT_SECONDS = 10.0
    _GRACE_SECONDS = 0.25

    def test_other_threads_wait_for_the_whole_populate(
        self, fake_package: Path, lifecycle_log: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        gates = {name: threading.Event() for name in ("slow_loading", "release", "competitor_started", "other_loaded")}
        monkeypatch.setattr(builtins, "_populate_gates", gates, raising=False)
        make_fake_module(
            fake_package,
            "slow",
            manifest={"title": "slow", "version": "1"},
            bootstrap=(
                "import builtins\n\n\n"
                "def on_load(*, app):\n"
                "    gates = builtins._populate_gates\n"
                "    gates['slow_loading'].set()\n"
                "    if not gates['release'].wait(timeout=10):\n"
                "        raise RuntimeError('the test never released the slow module')\n"
                "    builtins._lifecycle_calls.append('on_load:slow')\n\n\n"
                "def ready(*, app):\n"
                "    builtins._lifecycle_calls.append('ready:slow')\n"
            ),
        )
        make_fake_module(
            fake_package,
            "other",
            manifest={"title": "other", "version": "1"},
            bootstrap=(
                "import builtins\n\n\n"
                "def on_load(*, app):\n"
                "    builtins._populate_gates['other_loaded'].set()\n"
                "    builtins._lifecycle_calls.append('on_load:other')\n"
            ),
        )
        registry = ModuleRegistry()
        errors: list[BaseException] = []

        def run(action: Any) -> None:
            try:
                action()
            except BaseException as exc:  # reported on the main thread below
                errors.append(exc)

        def competitor() -> None:
            gates["competitor_started"].set()
            registry.install("other")

        populate_thread = threading.Thread(target=run, args=(lambda: registry.populate(["slow"]),), daemon=True)
        competitor_thread = threading.Thread(target=run, args=(competitor,), daemon=True)

        def diagnostics() -> str:
            states = {name: event.is_set() for name, event in gates.items()}
            return f"gates={states} calls={lifecycle_log} errors={errors!r}"

        try:
            populate_thread.start()
            assert gates["slow_loading"].wait(self._WAIT_SECONDS), f"populate never reached on_load: {diagnostics()}"
            competitor_thread.start()
            assert gates["competitor_started"].wait(self._WAIT_SECONDS), f"competitor never started: {diagnostics()}"
            # A bounded negative wait: a lock regression is caught here, and a slow machine cannot fail it.
            assert not gates["other_loaded"].wait(self._GRACE_SECONDS), (
                f"install('other') ran while populate() was still running: {diagnostics()}"
            )
        finally:
            gates["release"].set()
            populate_thread.join(self._WAIT_SECONDS)
            competitor_thread.join(self._WAIT_SECONDS)

        assert not populate_thread.is_alive(), f"populate thread did not finish: {diagnostics()}"
        assert not competitor_thread.is_alive(), f"competitor thread did not finish: {diagnostics()}"
        assert errors == [], diagnostics()
        assert lifecycle_log == ["on_load:slow", "ready:slow", "on_load:other"], diagnostics()
