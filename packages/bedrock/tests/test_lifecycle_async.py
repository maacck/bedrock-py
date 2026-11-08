"""Async and sync lifecycle entrypoints of ``ModuleRegistry`` share one state machine."""

from __future__ import annotations

import asyncio
import builtins
import gc
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from bedrock.module import (
    ModuleAsyncRequiredError,
    ModuleLifecycleError,
    ModuleLifecycleState,
    ModuleRegistry,
    ModuleStartupRollbackError,
)
from bedrock.module.signals import module_loaded

from tests.helpers import make_fake_module

pytestmark = pytest.mark.filterwarnings(
    "error::RuntimeWarning",
    "ignore::ResourceWarning",
    "error::pytest.PytestUnraisableExceptionWarning",
)

_BOOTSTRAP = """
import asyncio
import builtins
import threading

_NAME = __name__.rpartition(".")[0]
_KIND = builtins._hook_kinds.get(_NAME, "sync")


def _record(hook, app, registry):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    builtins._events.append(f"{hook}:{app.name}")
    builtins._hook_threads[(hook, app.name)] = threading.get_ident()
    builtins._hook_loops[(hook, app.name)] = loop
    action = builtins._hook_actions.get((hook, app.name))
    if action is not None:
        action(registry)
    if (hook, app.name) in builtins._hook_failures:
        raise RuntimeError(f"{hook} boom in {app.name}")


async def _arecord(hook, app, registry):
    await asyncio.sleep(0)
    _record(hook, app, registry)
    await asyncio.sleep(0)
    gate = builtins._hook_gates.get((hook, app.name))
    if gate is not None:
        await gate()


async def _tracked(hook, app):
    builtins._awaitable_started.append(f"{hook}:{app.name}")


def _define(hook):
    if _KIND == "syncawaitable":

        def impl(*, app, registry):
            _record(hook, app, registry)
            return _tracked(hook, app)

    elif _KIND == "async":

        async def impl(*, app, registry):
            await _arecord(hook, app, registry)

    elif _KIND == "awaitable":

        def impl(*, app, registry):
            return _arecord(hook, app, registry)

    else:

        def impl(*, app, registry):
            _record(hook, app, registry)

    impl.__name__ = hook
    return impl


on_load = _define("on_load")
ready = _define("ready")
on_shutdown = _define("on_shutdown")
"""


@pytest.fixture(autouse=True)
def hook_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Shared observable state for the fake modules; also fails a test that leaks a coroutine."""
    events: list[str] = []
    monkeypatch.setattr(builtins, "_events", events, raising=False)
    monkeypatch.setattr(builtins, "_hook_kinds", {}, raising=False)
    monkeypatch.setattr(builtins, "_hook_failures", set(), raising=False)
    monkeypatch.setattr(builtins, "_hook_actions", {}, raising=False)
    monkeypatch.setattr(builtins, "_hook_gates", {}, raising=False)
    monkeypatch.setattr(builtins, "_hook_threads", {}, raising=False)
    monkeypatch.setattr(builtins, "_hook_loops", {}, raising=False)
    monkeypatch.setattr(builtins, "_awaitable_started", [], raising=False)
    yield events
    gc.collect()


@pytest.fixture
def kinds() -> dict[str, str]:
    """Hook flavor per module: ``sync`` (default), ``async`` or ``awaitable`` (sync function returning one)."""
    return builtins._hook_kinds  # type: ignore[attr-defined, no-any-return]


@pytest.fixture
def failures() -> set[tuple[str, str]]:
    return builtins._hook_failures  # type: ignore[attr-defined, no-any-return]


@pytest.fixture
def actions() -> dict[tuple[str, str], Callable[[ModuleRegistry], Any]]:
    return builtins._hook_actions  # type: ignore[attr-defined, no-any-return]


@pytest.fixture
def gates() -> dict[tuple[str, str], Callable[[], Any]]:
    return builtins._hook_gates  # type: ignore[attr-defined, no-any-return]


def _chain(root: Path, *names: str) -> None:
    """Create modules where each name depends on the one before it."""
    previous: str | None = None
    for name in names:
        manifest: dict[str, Any] = {"title": name, "version": "1"}
        if previous is not None:
            manifest["depends_on"] = [previous]
        make_fake_module(root, name, manifest=manifest, bootstrap=_BOOTSTRAP)
        previous = name


def _states(registry: ModuleRegistry) -> dict[str, ModuleLifecycleState]:
    return {name: registry._app_configs[name].lifecycle_state for name in registry.list_installed()}


def _call(registry: ModuleRegistry, mode: str, operation: str, *args: Any) -> Any:
    """Run one registry operation through the sync entrypoint or, in a fresh loop, the async one."""
    if mode == "sync":
        return getattr(registry, operation)(*args)
    return asyncio.run(getattr(registry, f"a{operation}")(*args))


MODES = ["sync", "async"]
KINDS = ["sync", "async", "awaitable"]


class TestStartupAndShutdownOrder:
    @pytest.mark.parametrize("mode", MODES)
    @pytest.mark.parametrize("kind", KINDS)
    def test_uniform_graph(
        self, fake_package: Path, kinds: dict[str, str], hook_state: list[str], mode: str, kind: str
    ) -> None:
        _chain(fake_package, "life_a", "life_b", "life_c")
        kinds.update({"life_a": kind, "life_b": kind, "life_c": kind})
        registry = ModuleRegistry()

        _call(registry, mode, "populate", ["life_c"])
        _call(registry, mode, "shutdown")

        assert hook_state == [
            "on_load:life_a",
            "on_load:life_b",
            "on_load:life_c",
            "ready:life_a",
            "ready:life_b",
            "ready:life_c",
            "on_shutdown:life_c",
            "on_shutdown:life_b",
            "on_shutdown:life_a",
        ]
        assert set(_states(registry).values()) == {ModuleLifecycleState.SHUTDOWN}
        assert not registry._adapter.is_open

    @pytest.mark.parametrize("mode", MODES)
    def test_mixed_graph(self, fake_package: Path, kinds: dict[str, str], hook_state: list[str], mode: str) -> None:
        _chain(fake_package, "mix_a", "mix_b", "mix_c", "mix_d")
        kinds.update({"mix_a": "sync", "mix_b": "async", "mix_c": "awaitable", "mix_d": "sync"})
        registry = ModuleRegistry()

        _call(registry, mode, "populate", ["mix_d"])
        _call(registry, mode, "shutdown")

        assert [event for event in hook_state if event.startswith("on_load")] == [
            "on_load:mix_a",
            "on_load:mix_b",
            "on_load:mix_c",
            "on_load:mix_d",
        ]
        assert [event for event in hook_state if event.startswith("on_shutdown")] == [
            "on_shutdown:mix_d",
            "on_shutdown:mix_c",
            "on_shutdown:mix_b",
            "on_shutdown:mix_a",
        ]

    @pytest.mark.asyncio
    async def test_async_entrypoints_in_one_running_loop(
        self, fake_package: Path, kinds: dict[str, str], hook_state: list[str]
    ) -> None:
        _chain(fake_package, "arun_a", "arun_b", "arun_c")
        kinds.update({"arun_a": "async", "arun_b": "sync", "arun_c": "awaitable"})
        registry = ModuleRegistry()
        loop = asyncio.get_running_loop()
        main_thread = threading.get_ident()

        await registry.ainstall("arun_c")
        await registry.amark_ready()
        await registry.ashutdown()

        loops, threads = builtins._hook_loops, builtins._hook_threads  # type: ignore[attr-defined]
        assert loops[("on_load", "arun_a")] is loop
        assert loops[("on_load", "arun_c")] is loop
        assert loops[("on_load", "arun_b")] is None
        assert threads[("on_load", "arun_a")] == main_thread
        assert threads[("on_load", "arun_b")] != main_thread
        assert hook_state[-3:] == ["on_shutdown:arun_c", "on_shutdown:arun_b", "on_shutdown:arun_a"]

    def test_signals_run_in_both_modes(self, fake_package: Path) -> None:
        _chain(fake_package, "sig_a")
        seen: list[str] = []

        def sync_receiver(sender: Any, **kwargs: Any) -> None:
            seen.append("sync")

        async def async_receiver(sender: Any, **kwargs: Any) -> None:
            await asyncio.sleep(0)
            seen.append("async")

        module_loaded.connect(sync_receiver, weak=False)
        module_loaded.connect(async_receiver, weak=False)
        try:
            ModuleRegistry().install("sig_a")
            asyncio.run(ModuleRegistry().ainstall("sig_a"))
        finally:
            module_loaded.disconnect(sync_receiver)
            module_loaded.disconnect(async_receiver)

        assert sorted(seen) == ["async", "async", "sync", "sync"]


class TestAdapterLoopLifetime:
    def test_one_loop_serves_startup_and_shutdown_then_closes(self, fake_package: Path, kinds: dict[str, str]) -> None:
        _chain(fake_package, "loop_a", "loop_b")
        kinds.update({"loop_a": "async", "loop_b": "async"})
        registry = ModuleRegistry()

        registry.populate(["loop_b"])
        loops = builtins._hook_loops  # type: ignore[attr-defined]
        startup_loop = loops[("on_load", "loop_a")]
        assert startup_loop is loops[("ready", "loop_b")]
        assert not startup_loop.is_closed()

        registry.shutdown()

        assert loops[("on_shutdown", "loop_a")] is startup_loop
        assert startup_loop.is_closed()
        assert not registry._adapter.is_open

    def test_loop_stays_open_while_a_module_is_not_shut_down(
        self, fake_package: Path, kinds: dict[str, str], failures: set[tuple[str, str]]
    ) -> None:
        _chain(fake_package, "keep_a", "keep_b")
        kinds.update({"keep_a": "async", "keep_b": "async"})
        failures.add(("on_shutdown", "keep_b"))
        registry = ModuleRegistry()
        registry.populate(["keep_b"])

        with pytest.raises(ModuleLifecycleError):
            registry.shutdown()
        assert registry._adapter.is_open

        failures.clear()
        registry.shutdown()
        assert not registry._adapter.is_open

    def test_failed_first_install_does_not_keep_a_loop(
        self, fake_package: Path, kinds: dict[str, str], failures: set[tuple[str, str]]
    ) -> None:
        _chain(fake_package, "fail_a")
        kinds["fail_a"] = "async"
        failures.add(("on_load", "fail_a"))
        registry = ModuleRegistry()

        with pytest.raises(ModuleLifecycleError):
            registry.install("fail_a")

        assert registry.list_installed() == []
        assert not registry._adapter.is_open


class TestSyncEntrypointsInsideARunningLoop:
    @pytest.mark.asyncio
    async def test_all_sync_graph_still_works(self, fake_package: Path, hook_state: list[str]) -> None:
        _chain(fake_package, "inloop_a", "inloop_b")
        registry = ModuleRegistry()

        registry.populate(["inloop_b"])
        registry.shutdown()

        assert hook_state[-2:] == ["on_shutdown:inloop_b", "on_shutdown:inloop_a"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["async", "awaitable"])
    async def test_install_names_the_async_entrypoint_and_rolls_back(
        self, fake_package: Path, kinds: dict[str, str], hook_state: list[str], kind: str
    ) -> None:
        _chain(fake_package, "loopinst_a", "loopinst_b")
        kinds["loopinst_b"] = kind
        registry = ModuleRegistry()

        with pytest.raises(ModuleAsyncRequiredError, match=r"ainstall"):
            registry.install("loopinst_b")

        assert registry.list_installed() == []
        assert "on_load:loopinst_b" not in hook_state
        assert hook_state == ["on_load:loopinst_a", "on_shutdown:loopinst_a"]

        await registry.ainstall("loopinst_b")
        assert registry.list_installed() == ["loopinst_a", "loopinst_b"]
        await registry.ashutdown()

    @pytest.mark.asyncio
    async def test_populate_names_the_async_entrypoint(self, fake_package: Path, kinds: dict[str, str]) -> None:
        _chain(fake_package, "looppop_a")
        kinds["looppop_a"] = "async"

        with pytest.raises(ModuleAsyncRequiredError, match=r"apopulate"):
            ModuleRegistry().populate(["looppop_a"])

    @pytest.mark.asyncio
    async def test_mark_ready_and_shutdown_fail_before_any_hook_runs(
        self, fake_package: Path, kinds: dict[str, str], hook_state: list[str]
    ) -> None:
        _chain(fake_package, "lm_a", "lm_b")
        kinds["lm_b"] = "async"
        registry = ModuleRegistry()
        await registry.ainstall("lm_b")
        hook_state.clear()

        with pytest.raises(ModuleAsyncRequiredError, match=r"amark_ready"):
            registry.mark_ready()
        assert hook_state == []
        assert set(_states(registry).values()) == {ModuleLifecycleState.LOADED}

        await registry.amark_ready()
        hook_state.clear()
        with pytest.raises(ModuleAsyncRequiredError, match=r"ashutdown"):
            registry.shutdown()
        assert hook_state == []
        assert set(_states(registry).values()) == {ModuleLifecycleState.READY}

        await registry.ashutdown()
        assert set(_states(registry).values()) == {ModuleLifecycleState.SHUTDOWN}

    @pytest.mark.asyncio
    async def test_hook_returning_an_awaitable_is_discarded_without_a_leak(
        self, fake_package: Path, kinds: dict[str, str]
    ) -> None:
        _chain(fake_package, "ret_a")
        kinds["ret_a"] = "awaitable"
        registry = ModuleRegistry()
        await registry.ainstall("ret_a")

        with pytest.raises(ModuleAsyncRequiredError, match=r"amark_ready"):
            registry.mark_ready()
        assert _states(registry) == {"ret_a": ModuleLifecycleState.LOADED}

        with pytest.raises(ModuleLifecycleError, match=r"ashutdown"):
            registry.shutdown()
        assert _states(registry) == {"ret_a": ModuleLifecycleState.LOADED}

        await registry.amark_ready()
        await registry.ashutdown()
        assert _states(registry) == {"ret_a": ModuleLifecycleState.SHUTDOWN}

    @pytest.mark.asyncio
    async def test_async_receiver_is_not_called_from_sync_send(self, fake_package: Path) -> None:
        _chain(fake_package, "rcv_a")
        called: list[str] = []

        async def receiver(sender: Any, **kwargs: Any) -> None:
            called.append("called")

        module_loaded.connect(receiver, weak=False)
        try:
            registry = ModuleRegistry()
            with pytest.raises(ModuleAsyncRequiredError, match=r"ainstall"):
                registry.install("rcv_a")
        finally:
            module_loaded.disconnect(receiver)

        assert called == []
        assert registry.list_installed() == []


class TestSharedStateMachine:
    @pytest.mark.parametrize("mode", MODES)
    @pytest.mark.parametrize("kind", KINDS)
    def test_install_failure_rolls_back_introduced_modules_only(
        self,
        fake_package: Path,
        kinds: dict[str, str],
        failures: set[tuple[str, str]],
        hook_state: list[str],
        mode: str,
        kind: str,
    ) -> None:
        _chain(fake_package, "atom_a", "atom_b", "atom_c")
        kinds.update({"atom_a": kind, "atom_b": kind, "atom_c": kind})
        registry = ModuleRegistry()
        _call(registry, mode, "install", "atom_a")
        failures.add(("on_load", "atom_c"))

        with pytest.raises(ModuleLifecycleError, match="on_load.*atom_c"):
            _call(registry, mode, "install", "atom_c")

        assert registry.list_installed() == ["atom_a"]
        assert hook_state[-2:] == ["on_load:atom_c", "on_shutdown:atom_b"]
        assert registry._app_configs["atom_a"].lifecycle_state is ModuleLifecycleState.LOADED

    @pytest.mark.parametrize("mode", MODES)
    def test_rollback_failure_aggregates_every_failure(
        self, fake_package: Path, kinds: dict[str, str], failures: set[tuple[str, str]], mode: str
    ) -> None:
        _chain(fake_package, "agg_a", "agg_b", "agg_c")
        kinds.update({"agg_a": "async", "agg_b": "awaitable", "agg_c": "sync"})
        failures.update({("on_load", "agg_c"), ("on_shutdown", "agg_b"), ("on_shutdown", "agg_a")})
        registry = ModuleRegistry()

        with pytest.raises(ModuleStartupRollbackError) as caught:
            _call(registry, mode, "install", "agg_c")

        assert [(f.module, f.phase) for f in caught.value.failures] == [
            ("agg_c", "on_load"),
            ("agg_b", "on_shutdown"),
            ("agg_a", "on_shutdown"),
        ]
        assert registry.list_installed() == []

    @pytest.mark.parametrize("mode", MODES)
    def test_shutdown_attempts_everything_and_is_idempotent(
        self,
        fake_package: Path,
        kinds: dict[str, str],
        failures: set[tuple[str, str]],
        hook_state: list[str],
        mode: str,
    ) -> None:
        _chain(fake_package, "down_a", "down_b", "down_c")
        kinds.update({"down_a": "async", "down_b": "async", "down_c": "awaitable"})
        registry = ModuleRegistry()
        _call(registry, mode, "populate", ["down_c"])
        failures.add(("on_shutdown", "down_b"))
        hook_state.clear()

        with pytest.raises(ModuleLifecycleError) as caught:
            _call(registry, mode, "shutdown")

        assert hook_state == ["on_shutdown:down_c", "on_shutdown:down_b", "on_shutdown:down_a"]
        assert [f.module for f in caught.value.failures] == ["down_b"]
        assert _states(registry) == {
            "down_a": ModuleLifecycleState.SHUTDOWN,
            "down_b": ModuleLifecycleState.READY,
            "down_c": ModuleLifecycleState.SHUTDOWN,
        }

        failures.clear()
        hook_state.clear()
        _call(registry, mode, "shutdown")
        _call(registry, mode, "shutdown")
        assert hook_state == ["on_shutdown:down_b"]
        assert registry._shut_down is True

    @pytest.mark.parametrize("mode", MODES)
    def test_ready_failure_is_resumable(
        self,
        fake_package: Path,
        kinds: dict[str, str],
        failures: set[tuple[str, str]],
        hook_state: list[str],
        mode: str,
    ) -> None:
        _chain(fake_package, "rdy_a", "rdy_b")
        kinds.update({"rdy_a": "async", "rdy_b": "async"})
        failures.add(("ready", "rdy_b"))
        registry = ModuleRegistry()

        with pytest.raises(ModuleLifecycleError, match="Readiness failed"):
            _call(registry, mode, "populate", ["rdy_b"])
        assert _states(registry) == {"rdy_a": ModuleLifecycleState.READY, "rdy_b": ModuleLifecycleState.LOADED}
        assert registry.ready is False

        failures.clear()
        hook_state.clear()
        _call(registry, mode, "mark_ready")
        assert hook_state == ["ready:rdy_b"]
        assert registry.ready is True

    @pytest.mark.parametrize("mode", MODES)
    def test_install_after_ready_leaves_new_module_loaded(self, fake_package: Path, mode: str) -> None:
        _chain(fake_package, "late_a", "late_b")
        registry = ModuleRegistry()
        _call(registry, mode, "populate", ["late_a"])

        _call(registry, mode, "install", "late_b")

        assert registry.ready is True
        assert registry._app_configs["late_b"].lifecycle_state is ModuleLifecycleState.LOADED
        _call(registry, mode, "mark_ready")
        assert registry._app_configs["late_b"].lifecycle_state is ModuleLifecycleState.READY

    @pytest.mark.parametrize("mode", MODES)
    def test_populate_reaches_ready(self, fake_package: Path, mode: str) -> None:
        _chain(fake_package, "same_a")
        registry = ModuleRegistry()
        _call(registry, mode, "populate", ["same_a"])
        assert registry.get("same_a").lifecycle_state is ModuleLifecycleState.READY


class TestOperationGuards:
    @pytest.mark.asyncio
    async def test_async_hook_cannot_start_ready_or_shutdown_during_install(
        self, fake_package: Path, kinds: dict[str, str], actions: dict[tuple[str, str], Callable[..., Any]]
    ) -> None:
        _chain(fake_package, "grd_a")
        kinds["grd_a"] = "async"
        actions[("on_load", "grd_a")] = lambda registry: registry.mark_ready()
        registry = ModuleRegistry()

        with pytest.raises(ModuleLifecycleError, match=r"Cannot call mark_ready\(\) while install\(\) is running"):
            await registry.ainstall("grd_a")
        assert registry.list_installed() == []

    @pytest.mark.asyncio
    async def test_async_hook_awaiting_async_entrypoint_is_rejected(
        self, fake_package: Path, kinds: dict[str, str]
    ) -> None:
        _chain(fake_package, "grd2_a")
        kinds["grd2_a"] = "async"
        registry = ModuleRegistry()
        builtins._hook_gates[("on_load", "grd2_a")] = registry.ashutdown  # type: ignore[attr-defined]

        with pytest.raises(ModuleLifecycleError, match=r"Cannot call shutdown\(\) while install\(\) is running"):
            await registry.ainstall("grd2_a")
        assert registry.list_installed() == []

    @pytest.mark.asyncio
    async def test_worker_thread_started_by_a_hook_is_recognized_as_reentrant(
        self, fake_package: Path, kinds: dict[str, str], actions: dict[tuple[str, str], Callable[..., Any]]
    ) -> None:
        _chain(fake_package, "thr_a")
        actions[("on_load", "thr_a")] = lambda registry: registry.mark_ready()
        registry = ModuleRegistry()

        with pytest.raises(ModuleLifecycleError, match=r"Cannot call mark_ready\(\) while install\(\) is running"):
            await asyncio.wait_for(registry.ainstall("thr_a"), timeout=5)

    @pytest.mark.asyncio
    async def test_nested_ainstall_of_a_new_module_from_an_async_hook_is_atomic(
        self, fake_package: Path, kinds: dict[str, str], hook_state: list[str], failures: set[tuple[str, str]]
    ) -> None:
        make_fake_module(fake_package, "nest_inner", manifest={"title": "i", "version": "1"}, bootstrap=_BOOTSTRAP)
        make_fake_module(fake_package, "nest_outer", manifest={"title": "o", "version": "1"}, bootstrap=_BOOTSTRAP)
        kinds.update({"nest_inner": "async", "nest_outer": "async"})
        registry = ModuleRegistry()

        async def install_inner() -> None:
            await registry.ainstall("nest_inner")

        builtins._hook_gates[("on_load", "nest_outer")] = install_inner  # type: ignore[attr-defined]
        failures.add(("ready", "nest_outer"))

        await registry.ainstall("nest_outer")
        assert registry.list_installed() == ["nest_outer", "nest_inner"]

        builtins._hook_gates[("on_load", "nest_outer")] = None  # type: ignore[attr-defined]
        builtins._hook_gates.pop(("on_load", "nest_outer"))  # type: ignore[attr-defined]
        with pytest.raises(ModuleLifecycleError):
            await registry.amark_ready()
        await registry.ashutdown()
        assert hook_state.count("on_shutdown:nest_inner") == 1

    @pytest.mark.asyncio
    async def test_concurrent_tasks_are_serialized(
        self, fake_package: Path, kinds: dict[str, str], hook_state: list[str]
    ) -> None:
        make_fake_module(fake_package, "ser_a", manifest={"title": "a", "version": "1"}, bootstrap=_BOOTSTRAP)
        make_fake_module(fake_package, "ser_b", manifest={"title": "b", "version": "1"}, bootstrap=_BOOTSTRAP)
        kinds.update({"ser_a": "async", "ser_b": "async"})
        registry = ModuleRegistry()
        release = asyncio.Event()
        entered = asyncio.Event()

        async def hold() -> None:
            entered.set()
            await release.wait()

        builtins._hook_gates[("on_load", "ser_a")] = hold  # type: ignore[attr-defined]

        first = asyncio.create_task(registry.ainstall("ser_a"))
        await entered.wait()
        second = asyncio.create_task(registry.ainstall("ser_b"))
        for _ in range(10):
            await asyncio.sleep(0)
        assert "on_load:ser_b" not in hook_state
        assert not second.done()

        release.set()
        await asyncio.gather(first, second)

        assert hook_state[:2] == ["on_load:ser_a", "on_load:ser_b"]
        assert registry.list_installed() == ["ser_a", "ser_b"]

    @pytest.mark.asyncio
    async def test_concurrent_ashutdown_runs_each_hook_once(
        self, fake_package: Path, kinds: dict[str, str], hook_state: list[str]
    ) -> None:
        _chain(fake_package, "cs_a", "cs_b")
        kinds.update({"cs_a": "async", "cs_b": "async"})
        registry = ModuleRegistry()
        await registry.apopulate(["cs_b"])
        hook_state.clear()

        await asyncio.gather(registry.ashutdown(), registry.ashutdown(), registry.ashutdown())

        assert hook_state == ["on_shutdown:cs_b", "on_shutdown:cs_a"]

    @pytest.mark.asyncio
    async def test_sync_call_from_another_task_on_the_owner_thread_does_not_deadlock(
        self, fake_package: Path, kinds: dict[str, str]
    ) -> None:
        make_fake_module(fake_package, "dl_a", manifest={"title": "a", "version": "1"}, bootstrap=_BOOTSTRAP)
        kinds["dl_a"] = "async"
        registry = ModuleRegistry()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def hold() -> None:
            entered.set()
            await release.wait()

        builtins._hook_gates[("on_load", "dl_a")] = hold  # type: ignore[attr-defined]
        running = asyncio.create_task(registry.ainstall("dl_a"))
        await entered.wait()

        async def other_task() -> None:
            registry.shutdown()

        with pytest.raises(ModuleLifecycleError, match="would deadlock"):
            await asyncio.wait_for(other_task(), timeout=5)

        release.set()
        await running

    def test_other_thread_waits_for_a_running_async_operation(
        self, fake_package: Path, kinds: dict[str, str], hook_state: list[str]
    ) -> None:
        make_fake_module(fake_package, "thw_a", manifest={"title": "a", "version": "1"}, bootstrap=_BOOTSTRAP)
        make_fake_module(fake_package, "thw_b", manifest={"title": "b", "version": "1"}, bootstrap=_BOOTSTRAP)
        kinds.update({"thw_a": "async", "thw_b": "sync"})
        registry = ModuleRegistry()
        entered = threading.Event()
        release = threading.Event()
        results: list[str] = []

        async def hold() -> None:
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.005)

        builtins._hook_gates[("on_load", "thw_a")] = hold  # type: ignore[attr-defined]
        owner = threading.Thread(target=lambda: asyncio.run(registry.ainstall("thw_a")))
        owner.start()
        assert entered.wait(5)

        def other() -> None:
            registry.install("thw_b")
            results.append("done")

        waiter = threading.Thread(target=other)
        waiter.start()
        waiter.join(0.2)
        assert waiter.is_alive()
        assert "on_load:thw_b" not in hook_state

        release.set()
        owner.join(5)
        waiter.join(5)

        assert results == ["done"]
        assert registry.list_installed() == ["thw_a", "thw_b"]


class TestCancellation:
    @pytest.mark.asyncio
    async def test_cancelled_install_rolls_back_and_releases_the_gate(
        self, fake_package: Path, kinds: dict[str, str], hook_state: list[str]
    ) -> None:
        _chain(fake_package, "can_a", "can_b")
        kinds.update({"can_a": "async", "can_b": "async"})
        registry = ModuleRegistry()
        entered = asyncio.Event()

        async def hold() -> None:
            entered.set()
            await asyncio.sleep(60)

        builtins._hook_gates[("on_load", "can_b")] = hold  # type: ignore[attr-defined]
        task = asyncio.create_task(registry.ainstall("can_b"))
        await entered.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert registry.list_installed() == []
        assert "on_shutdown:can_a" in hook_state

        builtins._hook_gates.clear()  # type: ignore[attr-defined]
        await asyncio.wait_for(registry.ainstall("can_b"), timeout=5)
        assert registry.list_installed() == ["can_a", "can_b"]

    @pytest.mark.asyncio
    async def test_cancelled_shutdown_can_be_resumed(
        self, fake_package: Path, kinds: dict[str, str], hook_state: list[str]
    ) -> None:
        _chain(fake_package, "cs2_a", "cs2_b")
        kinds.update({"cs2_a": "async", "cs2_b": "async"})
        registry = ModuleRegistry()
        await registry.apopulate(["cs2_b"])
        entered = asyncio.Event()

        async def hold() -> None:
            entered.set()
            await asyncio.sleep(60)

        builtins._hook_gates[("on_shutdown", "cs2_b")] = hold  # type: ignore[attr-defined]
        task = asyncio.create_task(registry.ashutdown())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert _states(registry)["cs2_a"] is ModuleLifecycleState.READY

        builtins._hook_gates.clear()  # type: ignore[attr-defined]
        await asyncio.wait_for(registry.ashutdown(), timeout=5)
        assert set(_states(registry).values()) == {ModuleLifecycleState.SHUTDOWN}

    @pytest.mark.asyncio
    async def test_cancelled_waiter_does_not_block_the_gate(self, fake_package: Path, kinds: dict[str, str]) -> None:
        make_fake_module(fake_package, "cw_a", manifest={"title": "a", "version": "1"}, bootstrap=_BOOTSTRAP)
        make_fake_module(fake_package, "cw_b", manifest={"title": "b", "version": "1"}, bootstrap=_BOOTSTRAP)
        kinds.update({"cw_a": "async", "cw_b": "async"})
        registry = ModuleRegistry()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def hold() -> None:
            entered.set()
            await release.wait()

        builtins._hook_gates[("on_load", "cw_a")] = hold  # type: ignore[attr-defined]
        first = asyncio.create_task(registry.ainstall("cw_a"))
        await entered.wait()
        waiter = asyncio.create_task(registry.ainstall("cw_b"))
        await asyncio.sleep(0.01)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        release.set()
        await first
        await asyncio.wait_for(registry.ainstall("cw_b"), timeout=5)
        assert registry.list_installed() == ["cw_a", "cw_b"]


class _BlockingHook:
    """Event-controlled body for a plain (worker-thread) hook that tracks how many copies run at once."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.running = 0
        self.max_running = 0
        self.calls = 0

    def __call__(self, registry: ModuleRegistry) -> None:
        with self.lock:
            self.calls += 1
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        self.started.set()
        try:
            assert self.release.wait(10), "test never released the blocked hook"
        finally:
            with self.lock:
                self.running -= 1

    def reset_for_retry(self) -> None:
        """Let later calls pass straight through."""
        self.release.set()


async def _wait_until(predicate: Callable[[], bool], timeout: float = 5) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "condition not reached"
        await asyncio.sleep(0.001)


async def _settle(cycles: int = 20) -> None:
    for _ in range(cycles):
        await asyncio.sleep(0.002)


def _pending_tasks() -> list[asyncio.Task[Any]]:
    return [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]


class TestCancellationDrainsWorkerHooks:
    @pytest.mark.asyncio
    async def test_cancelled_install_waits_for_the_sync_on_load_worker_before_rollback(
        self, fake_package: Path, hook_state: list[str], actions: dict[tuple[str, str], Callable[..., Any]]
    ) -> None:
        _chain(fake_package, "drn_a", "drn_b")
        blocker = _BlockingHook()
        actions[("on_load", "drn_b")] = blocker
        registry = ModuleRegistry()

        task = asyncio.create_task(registry.ainstall("drn_b"))
        await _wait_until(blocker.started.is_set)
        task.cancel()
        await _settle()

        assert not task.done()
        assert registry.list_installed() == ["drn_a", "drn_b"]
        assert "on_shutdown:drn_a" not in hook_state

        task.cancel()
        task.cancel()
        await _settle()
        assert not task.done()
        assert registry.list_installed() == ["drn_a", "drn_b"]

        blocker.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert registry.list_installed() == []
        assert hook_state == ["on_load:drn_a", "on_load:drn_b", "on_shutdown:drn_a"]
        assert blocker.running == 0
        assert _pending_tasks() == []

    @pytest.mark.asyncio
    async def test_retry_waits_for_the_original_worker_and_never_overlaps_it(
        self, fake_package: Path, hook_state: list[str], actions: dict[tuple[str, str], Callable[..., Any]]
    ) -> None:
        _chain(fake_package, "ovl_a", "ovl_b")
        blocker = _BlockingHook()
        actions[("on_load", "ovl_b")] = blocker
        registry = ModuleRegistry()

        first = asyncio.create_task(registry.ainstall("ovl_b"))
        await _wait_until(blocker.started.is_set)
        first.cancel()
        retry = asyncio.create_task(registry.ainstall("ovl_b"))
        await _settle()

        assert not retry.done()
        assert blocker.calls == 1

        blocker.release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await asyncio.wait_for(retry, timeout=5)

        assert blocker.max_running == 1
        assert blocker.calls == 2
        assert registry.list_installed() == ["ovl_a", "ovl_b"]
        assert hook_state == [
            "on_load:ovl_a",
            "on_load:ovl_b",
            "on_shutdown:ovl_a",
            "on_load:ovl_a",
            "on_load:ovl_b",
        ]
        assert _pending_tasks() == []

    @pytest.mark.asyncio
    async def test_no_hook_effect_lands_after_the_rollback_completed(
        self, fake_package: Path, hook_state: list[str], actions: dict[tuple[str, str], Callable[..., Any]]
    ) -> None:
        _chain(fake_package, "late_a", "late_b")
        late: list[str] = []

        def slow_hook(registry: ModuleRegistry) -> None:
            started.set()
            release.wait(10)
            late.append("effect")

        started, release = threading.Event(), threading.Event()
        actions[("on_load", "late_b")] = slow_hook
        registry = ModuleRegistry()
        task = asyncio.create_task(registry.ainstall("late_b"))
        await _wait_until(started.is_set)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert late == ["effect"]
        assert registry.list_installed() == []

    @pytest.mark.asyncio
    async def test_cancelled_shutdown_waits_for_the_sync_on_shutdown_worker(
        self, fake_package: Path, hook_state: list[str], actions: dict[tuple[str, str], Callable[..., Any]]
    ) -> None:
        _chain(fake_package, "sdn_a", "sdn_b")
        registry = ModuleRegistry()
        await registry.apopulate(["sdn_b"])
        blocker = _BlockingHook()
        actions[("on_shutdown", "sdn_b")] = blocker
        hook_state.clear()

        task = asyncio.create_task(registry.ashutdown())
        await _wait_until(blocker.started.is_set)
        task.cancel()
        task.cancel()
        competitor = asyncio.create_task(registry.ashutdown())
        await _settle()

        assert not task.done()
        assert not competitor.done()
        assert hook_state == ["on_shutdown:sdn_b"]

        blocker.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(competitor, timeout=5)

        assert blocker.max_running == 1
        assert hook_state == ["on_shutdown:sdn_b", "on_shutdown:sdn_b", "on_shutdown:sdn_a"]
        assert set(_states(registry).values()) == {ModuleLifecycleState.SHUTDOWN}
        assert registry._shut_down is True
        assert _pending_tasks() == []

    @pytest.mark.asyncio
    async def test_cancelled_shutdown_leaves_the_interrupted_module_retryable(
        self, fake_package: Path, hook_state: list[str], actions: dict[tuple[str, str], Callable[..., Any]]
    ) -> None:
        _chain(fake_package, "rty_a", "rty_b")
        registry = ModuleRegistry()
        await registry.apopulate(["rty_b"])
        blocker = _BlockingHook()
        actions[("on_shutdown", "rty_b")] = blocker

        task = asyncio.create_task(registry.ashutdown())
        await _wait_until(blocker.started.is_set)
        task.cancel()
        blocker.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert _states(registry) == {
            "rty_a": ModuleLifecycleState.READY,
            "rty_b": ModuleLifecycleState.READY,
        }
        assert registry._shut_down is False

        await registry.ashutdown()
        assert set(_states(registry).values()) == {ModuleLifecycleState.SHUTDOWN}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("operation", ["install", "shutdown"])
    async def test_awaitable_returned_by_a_sync_hook_is_discarded_after_cancellation(
        self,
        fake_package: Path,
        kinds: dict[str, str],
        actions: dict[tuple[str, str], Callable[..., Any]],
        operation: str,
    ) -> None:
        _chain(fake_package, "aw_a", "aw_b")
        kinds["aw_b"] = "syncawaitable"
        registry = ModuleRegistry()
        blocker = _BlockingHook()
        hook = "on_load" if operation == "install" else "on_shutdown"
        if operation == "shutdown":
            await registry.apopulate(["aw_b"])
            builtins._awaitable_started.clear()  # type: ignore[attr-defined]
        actions[(hook, "aw_b")] = blocker

        task = asyncio.create_task(registry.ainstall("aw_b") if operation == "install" else registry.ashutdown())
        await _wait_until(blocker.started.is_set)
        task.cancel()
        await _settle()
        assert not task.done()

        blocker.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert builtins._awaitable_started == []  # type: ignore[attr-defined]
        assert _pending_tasks() == []

    @pytest.mark.asyncio
    async def test_cancelled_shutdown_waits_for_the_adapter_loop_to_close(
        self, fake_package: Path, kinds: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _chain(fake_package, "adp_a")
        kinds["adp_a"] = "async"
        registry = ModuleRegistry()
        await asyncio.to_thread(registry.populate, ["adp_a"])
        assert registry._adapter.is_open
        blocker = _BlockingHook()
        real_close = registry._adapter.close

        def slow_close() -> None:
            blocker(registry)
            real_close()

        monkeypatch.setattr(registry._adapter, "close", slow_close)
        task = asyncio.create_task(registry.ashutdown())
        await _wait_until(blocker.started.is_set)
        task.cancel()
        task.cancel()
        competitor = asyncio.create_task(registry.ainstall("adp_a"))
        await _settle()

        assert not task.done()
        assert not competitor.done()

        blocker.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(competitor, timeout=5)
        assert not registry._adapter.is_open
        assert _pending_tasks() == []

    @pytest.mark.asyncio
    async def test_worker_failure_while_cancelled_does_not_replace_the_cancellation(
        self, fake_package: Path, actions: dict[tuple[str, str], Callable[..., Any]], failures: set[tuple[str, str]]
    ) -> None:
        _chain(fake_package, "wf_a", "wf_b")
        blocker = _BlockingHook()
        actions[("on_load", "wf_b")] = blocker
        failures.add(("on_load", "wf_b"))
        registry = ModuleRegistry()

        task = asyncio.create_task(registry.ainstall("wf_b"))
        await _wait_until(blocker.started.is_set)
        task.cancel()
        blocker.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert registry.list_installed() == []

    @pytest.mark.asyncio
    async def test_rollback_failure_after_cancellation_still_removes_modules(
        self, fake_package: Path, actions: dict[tuple[str, str], Callable[..., Any]], failures: set[tuple[str, str]]
    ) -> None:
        _chain(fake_package, "rbf_a", "rbf_b")
        blocker = _BlockingHook()
        actions[("on_load", "rbf_b")] = blocker
        failures.add(("on_shutdown", "rbf_a"))
        registry = ModuleRegistry()

        task = asyncio.create_task(registry.ainstall("rbf_b"))
        await _wait_until(blocker.started.is_set)
        task.cancel()
        blocker.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert registry.list_installed() == []
        await registry.ainstall("rbf_a")
        assert registry.list_installed() == ["rbf_a"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("signal_name", ["module_loaded", "module_shutdown"])
    async def test_cancelled_operation_waits_for_a_running_sync_receiver(
        self, fake_package: Path, hook_state: list[str], signal_name: str
    ) -> None:
        from bedrock.module import signals

        signal = getattr(signals, signal_name)
        _chain(fake_package, "rcv_a", "rcv_b")
        registry = ModuleRegistry()
        blocker = _BlockingHook()
        later: list[str] = []

        def make_receiver() -> Callable[..., None]:
            # Receiver order follows set iteration, so whichever receiver runs first blocks; any later one records.
            def receiver(sender: Any, **kwargs: Any) -> None:
                if kwargs["module"].name != "rcv_b":
                    return
                if blocker.started.is_set():
                    later.append("ran")
                else:
                    blocker(registry)

            return receiver

        first_receiver, second_receiver = make_receiver(), make_receiver()
        if signal_name == "module_shutdown":
            await registry.apopulate(["rcv_b"])
        signal.connect(first_receiver, weak=False)
        signal.connect(second_receiver, weak=False)
        try:
            operation = registry.ainstall("rcv_b") if signal_name == "module_loaded" else registry.ashutdown()
            task = asyncio.create_task(operation)
            await _wait_until(blocker.started.is_set)
            task.cancel()
            task.cancel()
            await _settle()
            assert not task.done()
            if signal_name == "module_loaded":
                assert registry.list_installed() == ["rcv_a", "rcv_b"]

            blocker.release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            signal.disconnect(first_receiver)
            signal.disconnect(second_receiver)

        assert later == []
        assert blocker.running == 0
        if signal_name == "module_loaded":
            assert registry.list_installed() == []
        else:
            assert registry._app_configs["rcv_b"].lifecycle_state is ModuleLifecycleState.SHUTDOWN
            await registry.ashutdown()
            assert set(_states(registry).values()) == {ModuleLifecycleState.SHUTDOWN}
        assert _pending_tasks() == []


class TestRunInWorker:
    @pytest.mark.asyncio
    async def test_returns_result_and_raises_worker_exceptions(self) -> None:
        from bedrock.module._runtime import run_in_worker

        assert await run_in_worker(lambda value: value + 1, 1) == 2
        with pytest.raises(ZeroDivisionError):
            await run_in_worker(lambda: 1 / 0)

    @pytest.mark.asyncio
    async def test_repeated_cancellation_cannot_skip_the_drain(self) -> None:
        from bedrock.module._runtime import run_in_worker

        started, release = threading.Event(), threading.Event()
        finished: list[str] = []

        def work() -> None:
            started.set()
            release.wait(10)
            finished.append("done")

        task = asyncio.create_task(run_in_worker(work))
        await _wait_until(started.is_set)
        for _ in range(5):
            task.cancel()
            await asyncio.sleep(0.005)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished == ["done"]

    @pytest.mark.asyncio
    async def test_returned_future_is_cancelled_and_drained(self) -> None:
        from bedrock.module._runtime import run_in_worker

        started, release = threading.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        background = loop.create_future()

        def work() -> Any:
            started.set()
            release.wait(10)
            return background

        task = asyncio.create_task(run_in_worker(work))
        await _wait_until(started.is_set)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert background.cancelled()


class _AsyncBlock:
    """Awaitable gate for an async hook that records overlap and whether the hook itself was cancelled."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.running = 0
        self.max_running = 0
        self.cancelled = 0

    async def __call__(self) -> None:
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.running -= 1


async def _hold_forever() -> None:
    await asyncio.sleep(60)


async def _cancel_repeatedly(task: asyncio.Task[Any], times: int = 3) -> None:
    for _ in range(times):
        task.cancel()
        await asyncio.sleep(0.002)


class TestRollbackCancellation:
    @pytest.mark.asyncio
    async def test_repeated_cancel_during_coroutine_rollback_still_cleans_every_module(
        self,
        fake_package: Path,
        kinds: dict[str, str],
        gates: dict[tuple[str, str], Callable[[], Any]],
        hook_state: list[str],
    ) -> None:
        _chain(fake_package, "rbc_a", "rbc_b", "rbc_c")
        kinds.update({"rbc_a": "async", "rbc_b": "async", "rbc_c": "async"})
        cleaning = _AsyncBlock()
        gates[("on_load", "rbc_c")] = _hold_forever
        gates[("on_shutdown", "rbc_b")] = cleaning
        registry = ModuleRegistry()

        task = asyncio.create_task(registry.ainstall("rbc_c"))
        await _wait_until(lambda: "on_load:rbc_c" in hook_state)
        task.cancel()
        await asyncio.wait_for(cleaning.entered.wait(), timeout=5)
        competitor = asyncio.create_task(registry.ainstall("rbc_a"))
        await _cancel_repeatedly(task)
        await _settle()

        assert not task.done()
        assert not competitor.done()
        assert cleaning.cancelled == 0
        assert "on_shutdown:rbc_a" not in hook_state

        cleaning.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        gates.pop(("on_shutdown", "rbc_b"))
        await asyncio.wait_for(competitor, timeout=5)

        assert hook_state[:6] == [
            "on_load:rbc_a",
            "on_load:rbc_b",
            "on_load:rbc_c",
            "on_shutdown:rbc_b",
            "on_shutdown:rbc_a",
            "on_load:rbc_a",
        ]
        assert cleaning.cancelled == 0
        assert cleaning.max_running == 1
        assert registry.list_installed() == ["rbc_a"]
        assert _pending_tasks() == []

    @pytest.mark.asyncio
    async def test_cancel_during_rollback_after_an_ordinary_install_error_cleans_every_module(
        self,
        fake_package: Path,
        kinds: dict[str, str],
        gates: dict[tuple[str, str], Callable[[], Any]],
        failures: set[tuple[str, str]],
        hook_state: list[str],
    ) -> None:
        _chain(fake_package, "rbe_a", "rbe_b", "rbe_c")
        kinds.update({"rbe_a": "async", "rbe_b": "async", "rbe_c": "async"})
        cleaning = _AsyncBlock()
        gates[("on_shutdown", "rbe_b")] = cleaning
        failures.add(("on_load", "rbe_c"))
        registry = ModuleRegistry()

        task = asyncio.create_task(registry.ainstall("rbe_c"))
        await asyncio.wait_for(cleaning.entered.wait(), timeout=5)
        await _cancel_repeatedly(task, 1)
        await _settle()
        assert not task.done()
        assert cleaning.cancelled == 0

        cleaning.release.set()
        with pytest.raises(asyncio.CancelledError) as info:
            await task

        assert isinstance(info.value.__cause__, ModuleLifecycleError)
        assert "rbe_c" in str(info.value.__cause__)
        assert hook_state == [
            "on_load:rbe_a",
            "on_load:rbe_b",
            "on_load:rbe_c",
            "on_shutdown:rbe_b",
            "on_shutdown:rbe_a",
        ]
        assert registry.list_installed() == []
        assert _pending_tasks() == []

    @pytest.mark.asyncio
    async def test_repeated_cancel_during_thread_rollback_cleans_every_module(
        self,
        fake_package: Path,
        actions: dict[tuple[str, str], Callable[..., Any]],
        hook_state: list[str],
    ) -> None:
        _chain(fake_package, "rbt_a", "rbt_b", "rbt_c")
        loading, cleaning = _BlockingHook(), _BlockingHook()
        actions[("on_load", "rbt_c")] = loading
        actions[("on_shutdown", "rbt_b")] = cleaning
        registry = ModuleRegistry()

        task = asyncio.create_task(registry.ainstall("rbt_c"))
        await _wait_until(loading.started.is_set)
        task.cancel()
        loading.release.set()
        await _wait_until(cleaning.started.is_set)
        await _cancel_repeatedly(task)
        await _settle()
        assert not task.done()
        assert "on_shutdown:rbt_a" not in hook_state

        cleaning.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert hook_state == [
            "on_load:rbt_a",
            "on_load:rbt_b",
            "on_load:rbt_c",
            "on_shutdown:rbt_b",
            "on_shutdown:rbt_a",
        ]
        assert cleaning.max_running == 1 and cleaning.calls == 1
        assert registry.list_installed() == []
        assert _pending_tasks() == []

    @pytest.mark.asyncio
    async def test_cancel_during_thread_rollback_after_an_ordinary_install_error(
        self,
        fake_package: Path,
        actions: dict[tuple[str, str], Callable[..., Any]],
        failures: set[tuple[str, str]],
        hook_state: list[str],
    ) -> None:
        _chain(fake_package, "rbx_a", "rbx_b", "rbx_c")
        cleaning = _BlockingHook()
        actions[("on_shutdown", "rbx_b")] = cleaning
        failures.add(("on_load", "rbx_c"))
        registry = ModuleRegistry()

        task = asyncio.create_task(registry.ainstall("rbx_c"))
        await _wait_until(cleaning.started.is_set)
        task.cancel()
        await _settle()
        assert not task.done()
        cleaning.release.set()
        with pytest.raises(asyncio.CancelledError) as info:
            await task

        assert isinstance(info.value.__cause__, ModuleLifecycleError)
        assert hook_state[-2:] == ["on_shutdown:rbx_b", "on_shutdown:rbx_a"]
        assert registry.list_installed() == []

    @pytest.mark.asyncio
    async def test_failed_cleanup_is_reported_alongside_a_cancellation(
        self,
        fake_package: Path,
        kinds: dict[str, str],
        gates: dict[tuple[str, str], Callable[[], Any]],
        failures: set[tuple[str, str]],
        hook_state: list[str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _chain(fake_package, "rbf_a", "rbf_b", "rbf_c")
        kinds.update({"rbf_a": "async", "rbf_b": "async", "rbf_c": "async"})
        gates[("on_load", "rbf_c")] = _hold_forever
        failures.add(("on_shutdown", "rbf_b"))
        registry = ModuleRegistry()

        task = asyncio.create_task(registry.ainstall("rbf_c"))
        await _wait_until(lambda: "on_load:rbf_c" in hook_state)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as info:
            await task

        assert hook_state[-2:] == ["on_shutdown:rbf_b", "on_shutdown:rbf_a"]
        assert any("rbf_b" in note for note in getattr(info.value, "__notes__", []))
        assert registry.list_installed() == []

    @pytest.mark.asyncio
    async def test_failed_cleanup_after_an_install_error_and_a_cancel_keeps_the_aggregate_as_cause(
        self,
        fake_package: Path,
        kinds: dict[str, str],
        gates: dict[tuple[str, str], Callable[[], Any]],
        failures: set[tuple[str, str]],
        hook_state: list[str],
    ) -> None:
        _chain(fake_package, "rba_a", "rba_b", "rba_c")
        kinds.update({"rba_a": "async", "rba_b": "async", "rba_c": "async"})
        cleaning = _AsyncBlock()
        gates[("on_shutdown", "rba_b")] = cleaning
        failures.update({("on_load", "rba_c"), ("on_shutdown", "rba_a")})
        registry = ModuleRegistry()

        task = asyncio.create_task(registry.ainstall("rba_c"))
        await asyncio.wait_for(cleaning.entered.wait(), timeout=5)
        task.cancel()
        cleaning.release.set()
        with pytest.raises(asyncio.CancelledError) as info:
            await task

        cause = info.value.__cause__
        assert isinstance(cause, ModuleStartupRollbackError)
        assert {(failure.module, failure.phase) for failure in cause.failures} == {
            ("rba_c", "on_load"),
            ("rba_a", "on_shutdown"),
        }
        assert registry.list_installed() == []

    @pytest.mark.asyncio
    async def test_a_cleanup_hook_that_raises_cancellation_keeps_ownership_of_unfinished_modules(
        self,
        fake_package: Path,
        kinds: dict[str, str],
        gates: dict[tuple[str, str], Callable[[], Any]],
        failures: set[tuple[str, str]],
        hook_state: list[str],
    ) -> None:
        _chain(fake_package, "rbo_a", "rbo_b", "rbo_c")
        kinds.update({"rbo_a": "async", "rbo_b": "async", "rbo_c": "async"})
        failures.add(("on_load", "rbo_c"))

        async def provider_cancelled() -> None:
            raise asyncio.CancelledError

        gates[("on_shutdown", "rbo_b")] = provider_cancelled
        registry = ModuleRegistry()

        with pytest.raises(asyncio.CancelledError):
            await registry.ainstall("rbo_c")

        assert registry.list_installed() == ["rbo_a", "rbo_b"]
        assert _states(registry) == {
            "rbo_a": ModuleLifecycleState.LOADED,
            "rbo_b": ModuleLifecycleState.LOADED,
        }
        assert "on_shutdown:rbo_a" not in hook_state

        gates.clear()
        await asyncio.wait_for(registry.ashutdown(), timeout=5)
        assert set(_states(registry).values()) == {ModuleLifecycleState.SHUTDOWN}
        assert hook_state[-2:] == ["on_shutdown:rbo_b", "on_shutdown:rbo_a"]
        assert _pending_tasks() == []

    @pytest.mark.asyncio
    async def test_cancel_after_install_succeeded_in_rollback_free_path_is_unchanged(
        self, fake_package: Path, kinds: dict[str, str], gates: dict[tuple[str, str], Callable[[], Any]]
    ) -> None:
        _chain(fake_package, "rbn_a", "rbn_b")
        kinds.update({"rbn_a": "async", "rbn_b": "async"})
        entered = _AsyncBlock()
        gates[("on_load", "rbn_b")] = entered
        registry = ModuleRegistry()

        task = asyncio.create_task(registry.ainstall("rbn_b"))
        await asyncio.wait_for(entered.entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert entered.cancelled == 1
        assert registry.list_installed() == []
