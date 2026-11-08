"""Behavior of bedrock-celery tasks and of the worker lifecycle in real Celery workers."""

import asyncio
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import bedrock
import pytest
from bedrock.database import DbSettings, db
from bedrock.di import Lifetime, container
from bedrock.testing.runtime import isolated_runtime
from bedrock_celery import BedrockTask
from celery import Celery
from sqlalchemy import String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

PROBE_BOOTSTRAP = """
import asyncio
import os

from . import state


async def on_load(**kwargs):
    state.loop = asyncio.get_running_loop()
    state.log(f"setup {os.getpid()}")


async def on_shutdown(**kwargs):
    state.log(f"shutdown {os.getpid()}")
"""

PROBE_STATE = """
import os

loop = None


def log(line):
    path = os.environ.get("PROBE_LOG")
    if path:
        with open(path, "a") as f:
            f.write(line + "\\n")
"""


class Base(DeclarativeBase):
    """Declarative base for the task tests."""


class Item(Base):
    """Row written by tasks."""

    __tablename__ = "celery_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))


class Tracker:
    """SCOPED service that records its instances and whether the scope closed them."""

    instances: list["Tracker"] = []

    def __init__(self) -> None:
        self.closed = False
        Tracker.instances.append(self)

    def close(self) -> None:
        self.closed = True


app = Celery("tests", set_as_current=False, task_cls=BedrockTask)


@app.task
def write_items(fail: bool = False) -> list[bool]:
    """Commit one item, leave a second uncommitted, and report whether the task shared one Tracker."""
    db.session.add(Item(name="committed"))
    db.session.commit()
    db.session.add(Item(name="uncommitted"))
    db.session.flush()
    if fail:
        raise ValueError("task failed")
    return [container.resolve(Tracker) is container.resolve(Tracker)]


@app.task(bind=True)
async def awrite_items(self) -> dict[str, object]:
    """Async counterpart of ``write_items`` that also reports its request and event loop."""
    from probe_app import state

    db.asession.add(Item(name="committed"))
    await db.asession.commit()
    db.asession.add(Item(name="uncommitted"))
    await db.asession.flush()
    await asyncio.sleep(0)
    return {
        "task_id": self.request.id,
        "same_loop": asyncio.get_running_loop() is state.loop,
        "same_tracker": container.resolve(Tracker) is container.resolve(Tracker),
    }


@pytest.fixture(scope="module")
def probe_path(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Put an importable Bedrock module ``probe_app`` with async lifecycle hooks on ``sys.path``."""
    root = tmp_path_factory.mktemp("probe")
    package = root / "probe_app"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "manifest.yaml").write_text('title: Probe\nversion: "0.1.0"\n')
    (package / "bootstrap.py").write_text(PROBE_BOOTSTRAP)
    (package / "state.py").write_text(PROBE_STATE)
    sys.path.insert(0, str(root))
    yield root
    sys.path.remove(str(root))


@pytest.fixture
def runtime(probe_path: Path, tmp_path: Path) -> Iterator[None]:
    """Give the test a fresh Bedrock runtime, a database, and a registered SCOPED ``Tracker``."""
    with isolated_runtime():
        db.init(
            settings=DbSettings(
                TYPE="sqlite", DRIVER="pysqlite", ASYNC_DRIVER="aiosqlite", SCHEMA=str(tmp_path / "t.db")
            )
        )
        Base.metadata.create_all(db.engine)
        Tracker.instances.clear()
        container.register(Tracker, factory=Tracker, lifetime=Lifetime.SCOPED)
        try:
            yield
        finally:
            bedrock.shutdown()  # releases the async engine on the runtime loop, then closes the loop


def _stored_names() -> list[str]:
    with db.independent_session() as session:
        return list(session.scalars(select(Item.name)))


@pytest.mark.usefixtures("runtime")
def test_sync_task_keeps_committed_work_and_rolls_back_the_rest() -> None:
    assert write_items.apply().get() == [True]
    assert write_items.apply().get() == [True]

    assert _stored_names() == ["committed", "committed"]
    assert len(Tracker.instances) == 2
    assert all(tracker.closed for tracker in Tracker.instances)


@pytest.mark.usefixtures("runtime")
def test_failing_sync_task_keeps_only_what_it_committed() -> None:
    with pytest.raises(ValueError, match="task failed"):
        write_items.apply(kwargs={"fail": True}).get()

    assert _stored_names() == ["committed"]


@pytest.mark.usefixtures("runtime")
def test_async_task_runs_on_the_loop_setup_ran_async_hooks_on() -> None:
    bedrock.setup("probe_app")

    result = awrite_items.apply(task_id="task-1").get()

    assert result == {"task_id": "task-1", "same_loop": True, "same_tracker": True}
    assert _stored_names() == ["committed"]
    assert [tracker.closed for tracker in Tracker.instances] == [True]


@pytest.mark.usefixtures("runtime")
def test_async_tasks_from_several_threads_share_the_loop() -> None:
    bedrock.setup("probe_app")
    results: list[dict[str, object]] = []

    threads = [threading.Thread(target=lambda: results.append(awrite_items.apply().get())) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert [result["same_loop"] for result in results] == [True, True, True]
    assert _stored_names() == ["committed"] * 3


class AsyncWrite(app.Task):
    """Class-based async task."""

    name = "tests.async_write"

    async def run(self) -> str:
        db.asession.add(Item(name="committed"))
        await db.asession.commit()
        db.asession.add(Item(name="uncommitted"))
        await db.asession.flush()
        return "async"


class InheritedAsyncWrite(AsyncWrite):
    """Inherits the async ``run`` unchanged."""

    name = "tests.inherited_async_write"


class SyncOverride(AsyncWrite):
    """Overrides the async ``run`` with a sync one, which must get a sync session."""

    name = "tests.sync_override"

    def run(self) -> str:
        db.session.add(Item(name="committed"))
        db.session.commit()
        db.session.add(Item(name="uncommitted"))
        db.session.flush()
        return "sync"


@pytest.mark.usefixtures("runtime")
@pytest.mark.parametrize(("task_cls", "kind"), [(InheritedAsyncWrite, "async"), (SyncOverride, "sync")])
def test_class_based_tasks_bind_the_session_of_their_own_run(task_cls: type, kind: str) -> None:
    task = app.register_task(task_cls())

    assert task.apply().get() == kind
    assert task.apply().get() == kind

    assert _stored_names() == ["committed", "committed"]


WORKER_MODULE = """
import asyncio
import os

from bedrock.database import DbSettings, db
from bedrock_celery import BedrockTask, connect_worker
from celery import Celery

app = Celery("probe_worker", broker="filesystem://", backend="file://{results}", task_cls=BedrockTask)
app.conf.broker_transport_options = {{"data_folder_in": "{queue}", "data_folder_out": "{queue}", "control_folder": "{queue}"}}
db.init(settings=DbSettings(TYPE="sqlite", DRIVER="pysqlite", ASYNC_DRIVER="aiosqlite", SCHEMA="{database}"))
connect_worker("probe_app")


@app.task(bind=True)
async def where(self):
    from probe_app import state

    return {{"pid": os.getpid(), "same_loop": asyncio.get_running_loop() is state.loop}}
"""


def _logged_pids(log: Path, event: str) -> list[int]:
    if not log.exists():
        return []
    return [int(line.split()[1]) for line in log.read_text().splitlines() if line.startswith(event)]


def _wait_for(condition, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("Timed out waiting for the worker.")
        time.sleep(0.1)


@pytest.mark.parametrize(("pool", "children"), [("prefork", 2), ("solo", 0), ("threads", 0)])
def test_worker_sets_bedrock_up_in_each_process_that_runs_tasks(
    probe_path: Path, tmp_path: Path, pool: str, children: int
) -> None:
    queue, results, log = tmp_path / "queue", tmp_path / "results", tmp_path / "probe.log"
    queue.mkdir()
    results.mkdir()
    source = WORKER_MODULE.format(queue=queue, results=results, database=tmp_path / "worker.db")
    (tmp_path / "probe_worker.py").write_text(source)
    env = {**os.environ, "PROBE_LOG": str(log), "PYTHONPATH": os.pathsep.join([str(tmp_path), str(probe_path)])}
    worker = subprocess.Popen(
        [sys.executable, "-m", "celery", "-A", "probe_worker", "worker", "--pool", pool, "--concurrency", "2"]
        + ["--without-heartbeat", "--without-mingle", "--without-gossip", "--loglevel", "WARNING"],
        cwd=tmp_path,
        env=env,
    )
    client = Celery("client", broker="filesystem://", backend=f"file://{results}", set_as_current=False)
    client.conf.broker_transport_options = {
        "data_folder_in": str(queue),
        "data_folder_out": str(queue),
        "control_folder": str(queue),
    }
    try:
        _wait_for(lambda: len(_logged_pids(log, "setup")) == max(children, 1))
        answers = [client.send_task("probe_worker.where").get(timeout=30) for _ in range(4)]
    finally:
        worker.send_signal(signal.SIGTERM)
        worker.wait(timeout=30)

    setup_pids = _logged_pids(log, "setup")
    assert worker.pid not in setup_pids if children else setup_pids == [worker.pid]
    assert len(set(setup_pids)) == len(setup_pids) == max(children, 1)
    assert all(answer["same_loop"] and answer["pid"] in setup_pids for answer in answers)
    assert sorted(_logged_pids(log, "shutdown")) == sorted(setup_pids)


@pytest.mark.parametrize("pool", ["prefork", "solo", "threads"])
def test_worker_stops_when_bedrock_setup_fails(tmp_path: Path, pool: str) -> None:
    broken = tmp_path / "broken_app"
    broken.mkdir()
    (broken / "__init__.py").write_text("")
    (broken / "manifest.yaml").write_text('title: Broken\nversion: "0.1.0"\n')
    (broken / "bootstrap.py").write_text("def on_load(**kwargs):\n    raise RuntimeError('config missing')\n")
    (tmp_path / "queue").mkdir()
    source = WORKER_MODULE.format(queue=tmp_path / "queue", results=tmp_path, database=tmp_path / "worker.db")
    (tmp_path / "probe_worker.py").write_text(
        source.replace('connect_worker("probe_app")', 'connect_worker("broken_app")')
    )
    worker = subprocess.Popen(
        [sys.executable, "-m", "celery", "-A", "probe_worker", "worker", "--pool", pool, "--concurrency", "2"]
        + ["--without-heartbeat", "--without-mingle", "--without-gossip", "--loglevel", "WARNING"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _, stderr = worker.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        worker.kill()
        raise AssertionError("The worker kept running after Bedrock setup failed.") from None

    assert "Bedrock setup failed" in stderr
    assert "config missing" in stderr
