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


def _define(hook):
    if _KIND == "async":

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
