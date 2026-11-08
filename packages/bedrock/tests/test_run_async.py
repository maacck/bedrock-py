"""``bedrock.run_async()``: synchronous code runs async work on the loop the runtime set up on."""

from __future__ import annotations

import asyncio
import builtins
import threading
from collections.abc import Iterator
from pathlib import Path

import bedrock
import pytest
from bedrock._loop import runtime_loop
from bedrock.database import manager as manager_module
from bedrock.database.config import DbSettings
from bedrock.database.manager import DatabaseManager
from bedrock.module import ModuleLifecycleError
from bedrock.testing import BedrockRuntime, ModuleFactory
from sqlalchemy import select

pytestmark = pytest.mark.filterwarnings(
    "error::RuntimeWarning",
    "ignore::ResourceWarning",
    "error::pytest.PytestUnraisableExceptionWarning",
)

_BOOTSTRAP = """
import asyncio
import builtins


class Client:
    \"\"\"An async client bound to the loop that opened it, like an asyncpg or redis.asyncio pool.\"\"\"

    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.closed = False

    async def call(self):
        if asyncio.get_running_loop() is not self.loop:
            raise RuntimeError("client used on a different event loop")
        await asyncio.sleep(0)
        return "ok"


async def on_load(**kwargs):
    builtins._ra_client = Client()


async def on_shutdown(**kwargs):
    if builtins._ra_fail_shutdown:
        raise RuntimeError("shutdown boom")
    builtins._ra_client.closed = True
"""


@pytest.fixture
def client_module(module_factory: ModuleFactory, monkeypatch: pytest.MonkeyPatch) -> str:
    """A module whose async ``on_load`` opens a loop-bound client and whose ``on_shutdown`` closes it."""
    monkeypatch.setattr(builtins, "_ra_client", None, raising=False)
    monkeypatch.setattr(builtins, "_ra_fail_shutdown", False, raising=False)
    return module_factory("ra_clients", bootstrap=_BOOTSTRAP)


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[DatabaseManager]:
    """A fresh database manager installed as the ``db`` singleton."""
    fresh = DatabaseManager()
    monkeypatch.setattr(manager_module, "db", fresh)
    fresh.init(
        settings=DbSettings(TYPE="sqlite", DRIVER="pysqlite", ASYNC_DRIVER="aiosqlite", SCHEMA=str(tmp_path / "r.db"))
    )
    yield fresh
    fresh.close()


def _client():  # type: ignore[no-untyped-def]
    return builtins._ra_client  # type: ignore[attr-defined]


async def _query(db: DatabaseManager) -> int:
    async with db.aindependent_session() as session:
        return (await session.execute(select(1))).scalar_one()


@pytest.mark.usefixtures("bedrock_runtime")
def test_calls_reuse_the_clients_and_engine_on_the_setup_loop(client_module: str, db: DatabaseManager) -> None:
    bedrock.setup(client_module)

    assert [bedrock.run_async(_client().call()) for _ in range(3)] == ["ok", "ok", "ok"]
    assert [bedrock.run_async(_query(db)) for _ in range(3)] == [1, 1, 1]

    bedrock.shutdown()

    assert _client().closed
    assert not db.has_async_engine
    assert not runtime_loop.is_open


def test_works_without_setup_and_shutdown_starts_a_new_loop() -> None:
    async def loop() -> asyncio.AbstractEventLoop:
        return asyncio.get_running_loop()

    first = bedrock.run_async(loop())
    assert bedrock.run_async(loop()) is first

    bedrock.shutdown()

    assert first.is_closed()
    assert bedrock.run_async(loop()) is not first


def test_exceptions_propagate() -> None:
    async def fail() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        bedrock.run_async(fail())


@pytest.mark.asyncio
async def test_refuses_a_running_loop_without_leaving_the_coroutine_unawaited() -> None:
    with pytest.raises(RuntimeError, match="Await the awaitable"):
        bedrock.run_async(asyncio.sleep(0))


@pytest.mark.usefixtures("bedrock_runtime")
def test_refuses_to_bypass_a_loop_asetup_is_still_running_on(client_module: str) -> None:
    errors: list[BaseException] = []

    async def serve() -> None:
        await bedrock.asetup(client_module)
        try:
            await asyncio.to_thread(lambda: bedrock.run_async(_client().call()))
        except RuntimeError as exc:
            errors.append(exc)
        await bedrock.ashutdown()

    asyncio.run(serve())

    assert len(errors) == 1 and "asetup()" in str(errors[0])
    assert bedrock.run_async(asyncio.sleep(0, result="after")) == "after"


def test_a_worker_thread_of_the_loop_raises_instead_of_deadlocking() -> None:
    async def nested() -> None:
        await asyncio.to_thread(lambda: bedrock.run_async(asyncio.sleep(0)))

    with pytest.raises(RuntimeError, match="waiting for"):
        bedrock.run_async(nested())


@pytest.mark.usefixtures("bedrock_runtime")
@pytest.mark.parametrize("shutdown_from", ["await_ashutdown", "thread_shutdown"])
def test_shutdown_from_runtime_loop_work_refuses_before_shutting_down(client_module: str, shutdown_from: str) -> None:
    bedrock.setup(client_module)

    async def stop() -> None:
        if shutdown_from == "await_ashutdown":
            await bedrock.ashutdown()
        else:
            await asyncio.to_thread(bedrock.shutdown)

    with pytest.raises(RuntimeError, match="after bedrock.run_async"):
        bedrock.run_async(stop())

    assert not _client().closed
    bedrock.shutdown()
    assert _client().closed


def test_calls_from_several_threads_run_one_at_a_time() -> None:
    active, peak, results = 0, 0, []
    lock = threading.Lock()

    async def work(n: int) -> int:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        await asyncio.sleep(0.01)
        with lock:
            active -= 1
        return n

    threads = [threading.Thread(target=lambda n=n: results.append(bedrock.run_async(work(n)))) for n in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == [0, 1, 2, 3, 4]
    assert peak == 1


def test_failed_shutdown_keeps_the_loop_so_a_retry_closes_clients_on_it(
    bedrock_runtime: BedrockRuntime, client_module: str
) -> None:
    bedrock.setup(client_module)
    loop = _client().loop
    builtins._ra_fail_shutdown = True  # type: ignore[attr-defined]

    with pytest.raises(ModuleLifecycleError):
        bedrock.shutdown()

    assert not loop.is_closed()
    builtins._ra_fail_shutdown = False  # type: ignore[attr-defined]
    bedrock.shutdown()
    assert _client().closed
    assert loop.is_closed()
