"""Pytest fixtures for the Bedrock hook system."""

import pytest

from bedrock.hooks import HookNamespace, HookRegistry, hooks


@pytest.fixture
def clean_hooks() -> HookRegistry:
    """Yield the global hook registry and reset everything after each test."""
    yield hooks
    hooks.reset()


@pytest.fixture
def hook_registry() -> HookRegistry:
    """Yield a fresh isolated HookRegistry instance."""
    return HookRegistry()


@pytest.fixture
def hook_namespace():
    """Factory fixture that creates isolated namespaces, auto-cleaned after test."""
    created: list[tuple[HookRegistry, str]] = []

    def factory(name: str, registry: HookRegistry | None = None) -> HookNamespace:
        reg = registry or hooks
        ns = HookNamespace(name, registry=reg)
        created.append((reg, name))
        return ns

    yield factory

    for reg, name in created:
        reg.reset(namespace=name)
