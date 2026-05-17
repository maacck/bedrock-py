"""Pytest fixtures for the Bedrock DI container."""

import pytest

from bedrock.di import Container, container


@pytest.fixture
def clean_container() -> Container:
    """Yield the global container and reset all registrations after each test."""
    yield container
    container.reset()


@pytest.fixture
def di_container() -> Container:
    """Yield a fresh isolated Container instance (not the global singleton)."""
    return Container()


@pytest.fixture
def override_service():
    """Return the global container's ``override`` context-manager factory."""
    return container.override
