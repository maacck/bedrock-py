"""Failure-atomic install, retry-safe readiness, and resilient shutdown of ``ModuleRegistry``."""

from __future__ import annotations

import builtins
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
)
from bedrock.module.signals import module_loaded, module_ready, module_shutdown, registry_ready, registry_shutdown

from tests.helpers import make_fake_module

_BOOTSTRAP = """
import builtins


def _record(hook, app):
    builtins._lifecycle_calls.append(f"{hook}:{app.name}")
    if (hook, app.name) in builtins._lifecycle_failures:
        raise RuntimeError(f"{hook} boom in {app.name}")


def on_load(*, app):
    _record("on_load", app)


def ready(*, app):
    _record("ready", app)


def on_shutdown(*, app):
    _record("on_shutdown", app)
"""


@pytest.fixture(autouse=True)
def lifecycle_log(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every hook call as ``"<hook>:<module>"`` and let tests choose failing hooks."""
    calls: list[str] = []
    monkeypatch.setattr(builtins, "_lifecycle_calls", calls, raising=False)
    monkeypatch.setattr(builtins, "_lifecycle_failures", set(), raising=False)
    return calls


@pytest.fixture
def fail_hooks() -> set[tuple[str, str]]:
    """Mutable set of ``(hook, module)`` pairs whose hook currently raises."""
    return builtins._lifecycle_failures  # type: ignore[attr-defined, no-any-return]


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
        app: AppConfig = ModuleRegistry().install("transitions")
        app.lifecycle_state = start

        with pytest.raises(ModuleLifecycleError, match="cannot move from lifecycle state"):
            app.transition_to(target)

        assert app.lifecycle_state is start
