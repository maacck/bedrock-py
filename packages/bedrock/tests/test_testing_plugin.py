"""End-to-end check of the ``bedrock.testing`` plugin from a separate pytest run."""

from __future__ import annotations

import pytest

pytest_plugins = ["pytester"]

CONFTEST = """
import pytest
from bedrock.database import db
from bedrock.database.config import DbSettings
from sqlalchemy import text

pytest_plugins = ["bedrock.testing"]


@pytest.fixture(scope="session", autouse=True)
def _database(tmp_path_factory):
    path = tmp_path_factory.mktemp("db") / "app.db"
    db.init(settings=DbSettings(TYPE="sqlite", DRIVER="pysqlite", ASYNC_DRIVER="aiosqlite", SCHEMA=str(path)))
    with db.engine.begin() as connection:
        connection.execute(text("CREATE TABLE orders (sku VARCHAR(20))"))
    yield
    db.close()
"""

TESTS = """
from pathlib import Path

import bedrock
import pytest
from bedrock.database import db
from bedrock.testing import arollback_session
from sqlalchemy import text

MARKERS = Path(__file__).parent / "markers"
MARKERS.mkdir(exist_ok=True)
BOOTSTRAP = f'''
from pathlib import Path

def on_shutdown(*, app):
    Path({str(MARKERS)!r}, app.name).write_text("down")
'''
KINDS = ["loaded", "ready", "registry_ready", "shutdown", "registry_shutdown"]


def place_order(sku):
    with db.session_scope() as session:
        session.execute(text("INSERT INTO orders VALUES (:sku)"), {"sku": sku})


def test_first_run(bedrock_runtime, module_factory, db_session):
    module_factory("orders", bootstrap=BOOTSTRAP)
    bedrock.setup("orders")
    place_order("A1")
    assert db_session.scalar(text("SELECT count(*) FROM orders")) == 1
    bedrock_runtime.registry.shutdown()
    assert [event.kind for event in bedrock_runtime.events] == KINDS


def test_same_module_name_again(bedrock_runtime, module_factory, db_session):
    module_factory("orders", bootstrap=BOOTSTRAP)
    bedrock.setup("orders")
    assert db_session.scalar(text("SELECT count(*) FROM orders")) == 0
    bedrock_runtime.registry.shutdown()
    assert [event.kind for event in bedrock_runtime.events] == KINDS


@pytest.mark.asyncio
async def test_async_run(bedrock_runtime, module_factory):
    module_factory("orders", bootstrap=BOOTSTRAP)
    await bedrock.asetup("orders")
    async with arollback_session() as session:
        async with db.asession_scope() as scoped:
            await scoped.execute(text("INSERT INTO orders VALUES ('B1')"))
        assert await session.scalar(text("SELECT count(*) FROM orders")) == 1
    await bedrock_runtime.registry.ashutdown()
    assert [event.kind for event in bedrock_runtime.events] == KINDS
    with db.engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM orders")) == 0


def test_fails_without_shutdown(bedrock_runtime, module_factory):
    module_factory("leaky", bootstrap=BOOTSTRAP)
    bedrock.setup("leaky")
    assert False, "deliberate failure"


def test_previous_runtime_was_closed_and_restored():
    assert (MARKERS / "leaky").read_text() == "down"
    assert not bedrock.apps.is_installed("leaky")
"""


def test_plugin_isolates_consecutive_tests(pytester: pytest.Pytester) -> None:
    """Same-named modules, committed rows, async sessions and a failing test do not leak into later tests."""

    pytester.makeconftest(CONFTEST)
    pytester.makepyfile(test_app=TESTS)

    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")

    result.assert_outcomes(passed=4, failed=1)
    result.stdout.fnmatch_lines(["*test_fails_without_shutdown*deliberate failure*"])


SHUTDOWN_IMPORT_TESTS = """
from pathlib import Path

import bedrock
import pytest

MARKERS = Path(__file__).parent / "markers"
MARKERS.mkdir(exist_ok=True)
BOOTSTRAP = '''
def on_shutdown(*, app):
    from .service import release
    release(app.name)
'''
SERVICE = f'''
from pathlib import Path

def release(name):
    Path({str(MARKERS)!r}, name).write_text("released")
'''


def test_runtime_first(bedrock_runtime, module_factory):
    module_factory("runtime_first", bootstrap=BOOTSTRAP, files={"service.py": SERVICE})
    bedrock.setup("runtime_first")


def test_factory_first(module_factory, bedrock_runtime):
    module_factory("factory_first", bootstrap=BOOTSTRAP, files={"service.py": SERVICE})
    bedrock.setup("factory_first")


@pytest.mark.parametrize("name", ["runtime_first", "factory_first"])
def test_shutdown_imported_the_module_service(name):
    assert (MARKERS / name).read_text() == "released"
"""


def test_teardown_shutdown_can_import_temporary_modules(pytester: pytest.Pytester) -> None:
    """Automatic shutdown runs while temporary modules are still importable, whatever the fixture order."""

    pytester.makeconftest('pytest_plugins = ["bedrock.testing"]\n')
    pytester.makepyfile(test_app=SHUTDOWN_IMPORT_TESTS)

    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")

    result.assert_outcomes(passed=4)
