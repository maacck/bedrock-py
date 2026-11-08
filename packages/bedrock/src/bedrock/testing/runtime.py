"""Isolated Bedrock registries for runtime tests."""

from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal, NamedTuple

import pytest

import bedrock

from .. import conf
from .. import lifecycle as lifecycle_module
from .. import module as module_package
from .._loop import runtime_loop
from ..di import container
from ..hooks import hooks
from ..module import ModuleLifecycleState, ModuleRegistry, signals
from ..module import registry as registry_module


class LifecycleEvent(NamedTuple):
    """One lifecycle signal observed for the runtime's registry."""

    kind: Literal["loaded", "ready", "registry_ready", "shutdown", "registry_shutdown"]
    module: str | None


@dataclass
class BedrockRuntime:
    """An isolated registry and its recorded lifecycle events."""

    registry: ModuleRegistry
    events: list[LifecycleEvent]


def _reset_settings_proxies() -> None:
    """Drop the cached settings of every live ``SettingsProxy`` so the next access reads the environment."""
    for proxy in list(conf._PROXIES.values()):
        proxy._wrapped = None


# Each open ``isolated_runtime()``: its registry and the cleanups to run after it shuts down, innermost last.
_AFTER_SHUTDOWN: list[tuple[ModuleRegistry, list[Callable[[], object]]]] = []


def _defer_until_runtime_shutdown(callback: Callable[[], object], packages: Collection[str]) -> bool:
    """Run ``callback`` right after an open runtime shuts down modules from ``packages``.

    Fixtures that own resources the modules' ``on_shutdown`` hooks may still need (such as the temporary packages of
    ``module_factory``) use this so their cleanup does not depend on fixture teardown order. Only a runtime that still
    holds a module from ``packages`` that is not shut down takes the callback, so a runtime that outlives the test
    does not keep that test's packages alive.

    Args:
        callback: Cleanup to run after the runtime's teardown shutdown.
        packages: Top-level package names the cleanup removes.

    Returns:
        ``False`` when no open runtime holds such a module; the caller then cleans up immediately.
    """
    for registry, callbacks in reversed(_AFTER_SHUTDOWN):
        if any(
            name.split(".")[0] in packages and app.lifecycle_state is not ModuleLifecycleState.SHUTDOWN
            for name, app in registry._app_configs.items()
        ):
            callbacks.append(callback)
            return True
    return False


@contextmanager
def isolated_runtime() -> Iterator[BedrockRuntime]:
    """Temporarily isolate the global module runtime and restore process state.

    Yields:
        A fresh registry and its lifecycle event log.
    """
    _reset_settings_proxies()
    saved_environ = dict(os.environ)
    lifecycle_signals = (
        signals.module_loaded,
        signals.module_ready,
        signals.module_shutdown,
        signals.module_installed,
        signals.registry_ready,
        signals.registry_shutdown,
    )
    snapshots = [
        (
            signal,
            signal.receivers.copy(),
            {key: set(value) for key, value in signal._by_receiver.items()},
            {key: set(value) for key, value in signal._by_sender.items()},
            signal._weak_senders.copy(),
        )
        for signal in lifecycle_signals
    ]
    owners = (bedrock, module_package, registry_module, lifecycle_module)
    original_apps = [owner.apps for owner in owners]
    runtime = BedrockRuntime(ModuleRegistry(), [])
    after_shutdown: list[Callable[[], object]] = []

    def record(
        kind: Literal["loaded", "ready", "registry_ready", "shutdown", "registry_shutdown"],
    ) -> Callable[..., None]:
        def receiver(sender: object, **kwargs: Any) -> None:
            if kwargs["registry"] is runtime.registry:
                app = kwargs.get("module")
                runtime.events.append(LifecycleEvent(kind, app.name if app is not None else None))

        return receiver

    def restore_signals() -> None:
        for signal, receivers, by_receiver, by_sender, weak_senders in snapshots:
            signal.receivers = receivers
            signal._by_receiver = defaultdict(signal.set_class, by_receiver)
            signal._by_sender = defaultdict(signal.set_class, by_sender)
            signal._weak_senders = weak_senders

    def restore_environ() -> None:
        os.environ.clear()
        os.environ.update(saved_environ)

    def restore_apps_aliases() -> None:
        for owner, original in zip(owners, original_apps, strict=True):
            setattr(owner, "apps", original)  # noqa: B010

    errors: list[BaseException] = []

    def attempt(step: Callable[[], object]) -> None:
        try:
            step()
        except BaseException as exc:
            errors.append(exc)

    body_error: BaseException | None = None
    try:
        _AFTER_SHUTDOWN.append((runtime.registry, after_shutdown))
        for owner in owners:
            setattr(owner, "apps", runtime.registry)  # noqa: B010
        for signal, kind in (
            (signals.module_loaded, "loaded"),
            (signals.module_ready, "ready"),
            (signals.registry_ready, "registry_ready"),
            (signals.module_shutdown, "shutdown"),
            (signals.registry_shutdown, "registry_shutdown"),
        ):
            signal.connect(record(kind), weak=False)
        yield runtime
    except BaseException as exc:
        body_error = exc
        raise
    finally:

        def shutdown() -> None:
            # all() requires readiness; installed modules may also exist before mark_ready().
            if any(
                app.lifecycle_state is not ModuleLifecycleState.SHUTDOWN
                for app in runtime.registry._app_configs.values()
            ):
                runtime.registry.shutdown()

        def release_async_engine() -> None:
            # Pooled async connections belong to the runtime loop closed below, so a later test on a new loop must
            # not reuse them. aclose() keeps the synchronous engine and configuration of a session-scoped database.
            db = lifecycle_module._database_manager()
            if runtime_loop.is_open and db is not None and db.has_async_engine:
                runtime_loop.run(db.aclose())

        attempt(shutdown)
        _AFTER_SHUTDOWN[:] = [entry for entry in _AFTER_SHUTDOWN if entry[1] is not after_shutdown]
        for callback in after_shutdown:
            attempt(callback)
        attempt(restore_signals)
        attempt(release_async_engine)
        attempt(runtime_loop.close)
        attempt(lambda: setattr(lifecycle_module, "_async_setup_loop", None))
        attempt(container.reset)
        attempt(hooks.reset)
        attempt(restore_environ)
        attempt(_reset_settings_proxies)
        attempt(restore_apps_aliases)
        if errors and body_error is not None:
            # Do not mask the exception raised inside the ``with`` block; report restore failures beside it.
            for error in errors:
                body_error.add_note(f"Also failed while restoring the Bedrock runtime: {error!r}")
        elif errors:
            first, *rest = errors
            for other in rest:
                first.add_note(f"Also failed while restoring the Bedrock runtime: {other!r}")
            raise first


@pytest.fixture
def bedrock_runtime() -> Iterator[BedrockRuntime]:
    """Provide an isolated runtime for a test.

    Yields:
        A fresh registry and its lifecycle event log.
    """
    with isolated_runtime() as runtime:
        yield runtime
