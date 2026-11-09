"""Top-level ``setup``/``shutdown`` entrypoints: module hooks first, then process-owned database engines."""

from __future__ import annotations

import asyncio
import builtins
import gc
import threading
from collections.abc import Iterator
from pathlib import Path

import bedrock
import bedrock.lifecycle as lifecycle
import pytest
from bedrock._loop import runtime_loop
from bedrock.database import manager as manager_module
from bedrock.database.config import DbSettings
from bedrock.database.manager import DatabaseManager
from bedrock.module import (
    ModuleAsyncRequiredError,
    ModuleLifecycleError,
    ModuleLifecycleState,
    ModuleRegistry,
)
from bedrock.testing import BedrockRuntime, ModuleFactory
from sqlalchemy import event, select

pytestmark = pytest.mark.filterwarnings(
    "error::RuntimeWarning",
    "ignore::ResourceWarning",
    "error::pytest.PytestUnraisableExceptionWarning",
)

_BOOTSTRAP = """
import asyncio
import builtins

_NAME = __name__.rpartition(".")[0]
_ASYNC = _NAME in builtins._proc_async


def _record(hook):
    builtins._proc_events.append(f"{hook}:{_NAME}")
    if (hook, _NAME) in builtins._proc_failures:
        raise RuntimeError(f"{hook} boom in {_NAME}")


async def _arecord(hook):
    await asyncio.sleep(0)
    _record(hook)
    gate = builtins._proc_gates.get((hook, _NAME))
    if gate is not None:
        await gate()


if _ASYNC:

    async def on_load(*, app):
        await _arecord("on_load")

    async def on_shutdown(*, app):
        await _arecord("on_shutdown")

else:

    def on_load(*, app):
        _record("on_load")

    def on_shutdown(*, app):
        _record("on_shutdown")
"""


@pytest.fixture(autouse=True)
def proc_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    events: list[str] = []
    monkeypatch.setattr(builtins, "_proc_events", events, raising=False)
    monkeypatch.setattr(builtins, "_proc_async", set(), raising=False)
    monkeypatch.setattr(builtins, "_proc_failures", set(), raising=False)
    monkeypatch.setattr(builtins, "_proc_gates", {}, raising=False)
    yield events
    gc.collect()


@pytest.fixture
def registry(bedrock_runtime: BedrockRuntime) -> Iterator[ModuleRegistry]:
    """The isolated registry that ``bedrock.setup()`` and ``bedrock.shutdown()`` act on.

    Injected hook failures are cleared first, so the runtime's teardown can finish shutting down modules that a
    test left behind after a deliberate shutdown failure.
    """
    yield bedrock_runtime.registry
    builtins._proc_failures.clear()


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> DatabaseManager:
    """A fresh database manager installed as the ``db`` singleton."""
    fresh = DatabaseManager()
    monkeypatch.setattr(manager_module, "db", fresh)
    return fresh


def _modules(factory: ModuleFactory) -> None:
    factory("proc_a", manifest={"title": "a", "version": "1"}, bootstrap=_BOOTSTRAP)
    factory("proc_b", manifest={"title": "b", "version": "1", "depends_on": ["proc_a"]}, bootstrap=_BOOTSTRAP)


def _open_engines(
    db: DatabaseManager, tmp_path: Path, events: list[str], *, sync: bool = True, asynchronous: bool = True
) -> None:
    """Initialize ``db``, check a pooled connection out and back in on each engine, and log pool closes."""
    db.init(
        settings=DbSettings(TYPE="sqlite", DRIVER="pysqlite", ASYNC_DRIVER="aiosqlite", SCHEMA=str(tmp_path / "p.db"))
    )
    event.listen(db.engine, "close", lambda *_: events.append("sync_connection_closed"))
    with db.engine.connect() as connection:
        connection.exec_driver_sql("select 1")
    if asynchronous:
        event.listen(db.aengine.sync_engine, "close", lambda *_: events.append("async_connection_closed"))

        async def use() -> None:
            async with db.aengine.connect() as connection:
                await connection.scalar(select(1))

        asyncio.run(use())
    if not sync:
        db.close()


def _shutdown(mode: str) -> None:
    if mode == "sync":
        bedrock.shutdown()
    else:
        asyncio.run(bedrock.ashutdown())


MODES = ["sync", "async"]


class TestPublicSurface:
    def test_top_level_exports(self) -> None:
        assert {"setup", "asetup", "shutdown", "ashutdown"} <= set(bedrock.__all__)
        assert bedrock.setup is lifecycle.setup
        assert bedrock.ashutdown is lifecycle.ashutdown

    def test_setup_is_idempotent_and_shutdown_stops_modules(
        self, module_factory: ModuleFactory, registry: ModuleRegistry, proc_state: list[str]
    ) -> None:
        _modules(module_factory)

        bedrock.setup("proc_b")
        bedrock.setup("proc_b")
        assert registry.ready is True
        assert proc_state == ["on_load:proc_a", "on_load:proc_b"]

        bedrock.shutdown()
        assert proc_state[2:] == ["on_shutdown:proc_b", "on_shutdown:proc_a"]

    @pytest.mark.asyncio
    async def test_asetup_is_idempotent_and_ashutdown_stops_modules(
        self, module_factory: ModuleFactory, registry: ModuleRegistry, proc_state: list[str]
    ) -> None:
        _modules(module_factory)
        builtins._proc_async.update({"proc_a", "proc_b"})  # type: ignore[attr-defined]

        await bedrock.asetup("proc_b")
        await bedrock.asetup("proc_b")
        assert proc_state == ["on_load:proc_a", "on_load:proc_b"]

        await bedrock.ashutdown()
        assert proc_state[2:] == ["on_shutdown:proc_b", "on_shutdown:proc_a"]
        assert {app.lifecycle_state for app in registry.all()} == {ModuleLifecycleState.SHUTDOWN}


class TestDatabaseReleasedAfterModules:
    @pytest.mark.parametrize("mode", MODES)
    def test_both_engines_are_disposed_after_every_module_hook(
        self,
        module_factory: ModuleFactory,
        tmp_path: Path,
        registry: ModuleRegistry,
        db: DatabaseManager,
        proc_state: list[str],
        mode: str,
    ) -> None:
        _modules(module_factory)
        builtins._proc_async.add("proc_a")  # type: ignore[attr-defined]
        registry.populate(["proc_b"])
        _open_engines(db, tmp_path, proc_state)
        proc_state.clear()
        threads_before = {t.ident for t in threading.enumerate()}

        _shutdown(mode)

        assert proc_state[:2] == ["on_shutdown:proc_b", "on_shutdown:proc_a"]
        assert sorted(proc_state[2:]) == ["async_connection_closed", "sync_connection_closed"]
        assert db.has_async_engine is False
        assert db._engine is None
        assert {t.ident for t in threading.enumerate()} <= threads_before

    @pytest.mark.parametrize("mode", MODES)
    def test_repeated_shutdown_does_nothing_more(
        self,
        module_factory: ModuleFactory,
        tmp_path: Path,
        registry: ModuleRegistry,
        db: DatabaseManager,
        proc_state: list[str],
        mode: str,
    ) -> None:
        _modules(module_factory)
        registry.populate(["proc_b"])
        _open_engines(db, tmp_path, proc_state)
        _shutdown(mode)
        proc_state.clear()

        _shutdown(mode)
        _shutdown(mode)

        assert proc_state == []

    @pytest.mark.parametrize("mode", MODES)
    def test_database_is_released_even_when_a_module_fails(
        self,
        module_factory: ModuleFactory,
        tmp_path: Path,
        registry: ModuleRegistry,
        db: DatabaseManager,
        proc_state: list[str],
        mode: str,
    ) -> None:
        _modules(module_factory)
        registry.populate(["proc_b"])
        _open_engines(db, tmp_path, proc_state)
        builtins._proc_failures.add(("on_shutdown", "proc_b"))  # type: ignore[attr-defined]
        proc_state.clear()

        with pytest.raises(ModuleLifecycleError) as caught:
            _shutdown(mode)

        assert [(f.module, f.phase) for f in caught.value.failures] == [("proc_b", "on_shutdown")]
        assert proc_state[:2] == ["on_shutdown:proc_b", "on_shutdown:proc_a"]
        assert sorted(proc_state[2:]) == ["async_connection_closed", "sync_connection_closed"]

        builtins._proc_failures.clear()  # type: ignore[attr-defined]
        proc_state.clear()
        _shutdown(mode)
        assert proc_state == ["on_shutdown:proc_b"]

    @pytest.mark.parametrize("mode", MODES)
    def test_database_failures_are_aggregated_and_every_step_runs(
        self,
        module_factory: ModuleFactory,
        tmp_path: Path,
        registry: ModuleRegistry,
        db: DatabaseManager,
        proc_state: list[str],
        monkeypatch: pytest.MonkeyPatch,
        mode: str,
    ) -> None:
        _modules(module_factory)
        registry.populate(["proc_b"])
        _open_engines(db, tmp_path, proc_state)
        builtins._proc_failures.add(("on_shutdown", "proc_a"))  # type: ignore[attr-defined]
        real_close = db.close

        def failing_close() -> None:
            real_close()
            raise RuntimeError("sync dispose boom")

        monkeypatch.setattr(db, "close", failing_close)

        with pytest.raises(ModuleLifecycleError) as caught:
            _shutdown(mode)

        assert [(f.module, f.phase) for f in caught.value.failures] == [
            ("proc_a", "on_shutdown"),
            ("bedrock.database", "database_close"),
        ]
        assert "async_connection_closed" in proc_state
        assert db.has_async_engine is False

    @pytest.mark.parametrize("mode", MODES)
    def test_async_release_failure_is_reported(
        self,
        module_factory: ModuleFactory,
        tmp_path: Path,
        registry: ModuleRegistry,
        db: DatabaseManager,
        proc_state: list[str],
        monkeypatch: pytest.MonkeyPatch,
        mode: str,
    ) -> None:
        _modules(module_factory)
        registry.populate(["proc_b"])
        _open_engines(db, tmp_path, proc_state)

        async def failing_aclose() -> None:
            raise RuntimeError("async dispose boom")

        monkeypatch.setattr(db, "aclose", failing_aclose)

        with pytest.raises(ModuleLifecycleError) as caught:
            _shutdown(mode)

        assert [(f.module, f.phase) for f in caught.value.failures] == [("bedrock.database", "database_aclose")]
        assert "sync_connection_closed" in proc_state
        assert registry.all()[0].lifecycle_state is ModuleLifecycleState.SHUTDOWN

    @pytest.mark.parametrize("mode", MODES)
    def test_never_initialized_database_is_a_no_op(
        self,
        module_factory: ModuleFactory,
        registry: ModuleRegistry,
        db: DatabaseManager,
        monkeypatch: pytest.MonkeyPatch,
        mode: str,
    ) -> None:
        _modules(module_factory)
        registry.populate(["proc_b"])
        built: list[str] = []
        monkeypatch.setattr(manager_module, "build_async_engine", lambda *a, **k: built.append("async"))
        monkeypatch.setattr(manager_module, "build_session_local_from_settings", lambda *a, **k: built.append("sync"))

        _shutdown(mode)

        assert built == []
        assert db._engine is None
        assert db._async_engine is None
        assert db._settings is None
        assert {app.lifecycle_state for app in registry.all()} == {ModuleLifecycleState.SHUTDOWN}

    @pytest.mark.parametrize("mode", MODES)
    def test_sync_only_database_never_touches_the_async_side(
        self,
        module_factory: ModuleFactory,
        tmp_path: Path,
        registry: ModuleRegistry,
        db: DatabaseManager,
        proc_state: list[str],
        monkeypatch: pytest.MonkeyPatch,
        mode: str,
    ) -> None:
        _modules(module_factory)
        registry.populate(["proc_b"])
        _open_engines(db, tmp_path, proc_state, asynchronous=False)
        calls: list[str] = []

        async def spy_aclose() -> None:
            calls.append("aclose")

        monkeypatch.setattr(db, "aclose", spy_aclose)
        proc_state.clear()

        _shutdown(mode)

        assert calls == []
        assert proc_state[-1] == "sync_connection_closed"
        assert db._async_engine is None

    @pytest.mark.parametrize("mode", MODES)
    def test_async_engine_alone_is_released(
        self,
        module_factory: ModuleFactory,
        tmp_path: Path,
        registry: ModuleRegistry,
        db: DatabaseManager,
        proc_state: list[str],
        mode: str,
    ) -> None:
        _modules(module_factory)
        registry.populate(["proc_b"])
        _open_engines(db, tmp_path, proc_state, sync=False)
        proc_state.clear()

        _shutdown(mode)

        assert proc_state[-1] == "async_connection_closed"
        assert db.has_async_engine is False

    def test_sync_shutdown_closes_an_async_engine_used_on_another_loop(
        self,
        module_factory: ModuleFactory,
        tmp_path: Path,
        registry: ModuleRegistry,
        db: DatabaseManager,
        proc_state: list[str],
    ) -> None:
        _modules(module_factory)
        registry.populate(["proc_b"])
        _open_engines(db, tmp_path, proc_state)

        bedrock.shutdown()

        assert "async_connection_closed" in proc_state
        assert not runtime_loop.is_open


class TestRunningLoop:
    @pytest.mark.asyncio
    async def test_sync_shutdown_with_async_engine_fails_before_mutating(
        self,
        module_factory: ModuleFactory,
        tmp_path: Path,
        registry: ModuleRegistry,
        db: DatabaseManager,
        proc_state: list[str],
    ) -> None:
        _modules(module_factory)
        await registry.apopulate(["proc_b"])
        db.init(
            settings=DbSettings(
                TYPE="sqlite", DRIVER="pysqlite", ASYNC_DRIVER="aiosqlite", SCHEMA=str(tmp_path / "r.db")
            )
        )
        async with db.aengine.connect() as connection:
            await connection.scalar(select(1))
        proc_state.clear()

        with pytest.raises(ModuleAsyncRequiredError, match=r"ashutdown"):
            bedrock.shutdown()

        assert proc_state == []
        assert db.has_async_engine is True
        assert {app.lifecycle_state for app in registry.all()} == {ModuleLifecycleState.READY}

        await bedrock.ashutdown()
        assert db.has_async_engine is False

    @pytest.mark.asyncio
    async def test_sync_shutdown_with_sync_engine_only_works_in_a_running_loop(
        self,
        module_factory: ModuleFactory,
        tmp_path: Path,
        registry: ModuleRegistry,
        db: DatabaseManager,
        proc_state: list[str],
    ) -> None:
        _modules(module_factory)
        registry.populate(["proc_b"])
        _open_engines(db, tmp_path, proc_state, asynchronous=False)
        proc_state.clear()

        bedrock.shutdown()

        assert proc_state == ["on_shutdown:proc_b", "on_shutdown:proc_a", "sync_connection_closed"]

    @pytest.mark.asyncio
    async def test_cancelled_ashutdown_leaves_the_database_open_until_retried(
        self,
        module_factory: ModuleFactory,
        tmp_path: Path,
        registry: ModuleRegistry,
        db: DatabaseManager,
        proc_state: list[str],
    ) -> None:
        _modules(module_factory)
        builtins._proc_async.update({"proc_a", "proc_b"})  # type: ignore[attr-defined]
        await registry.apopulate(["proc_b"])
        db.init(
            settings=DbSettings(
                TYPE="sqlite", DRIVER="pysqlite", ASYNC_DRIVER="aiosqlite", SCHEMA=str(tmp_path / "c.db")
            )
        )
        async with db.aengine.connect() as connection:
            await connection.scalar(select(1))
        entered = asyncio.Event()

        async def hold() -> None:
            entered.set()
            await asyncio.sleep(60)

        builtins._proc_gates[("on_shutdown", "proc_a")] = hold  # type: ignore[attr-defined]
        task = asyncio.create_task(bedrock.ashutdown())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert db.has_async_engine is True

        builtins._proc_gates.clear()  # type: ignore[attr-defined]
        await asyncio.wait_for(bedrock.ashutdown(), timeout=5)
        assert db.has_async_engine is False
        assert {app.lifecycle_state for app in registry.all()} == {ModuleLifecycleState.SHUTDOWN}


def test_summary_of_module_failure_without_database_keeps_the_original_error(
    module_factory: ModuleFactory, registry: ModuleRegistry, db: DatabaseManager
) -> None:
    _modules(module_factory)
    registry.populate(["proc_b"])
    builtins._proc_failures.add(("on_shutdown", "proc_b"))  # type: ignore[attr-defined]

    with pytest.raises(ModuleLifecycleError) as caught:
        bedrock.shutdown()

    assert [(f.module, f.phase) for f in caught.value.failures] == [("proc_b", "on_shutdown")]
    assert not isinstance(caught.value, ModuleAsyncRequiredError)
