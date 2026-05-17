"""Shared fixtures for Bedrock module runtime tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _clean_sys_modules() -> None:
    """Remove dynamically created fake packages from ``sys.modules`` after each test."""
    before = set(sys.modules.keys())
    yield
    after = set(sys.modules.keys())
    for key in after - before:
        del sys.modules[key]


@pytest.fixture
def fake_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a temporary directory already on ``sys.path`` for fake packages."""
    monkeypatch.syspath_prepend(str(tmp_path))
    return tmp_path
