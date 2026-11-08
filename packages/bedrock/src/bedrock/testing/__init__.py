"""Bedrock testing fixtures.

Register the package as a pytest plugin in the project's root ``conftest.py``::

    pytest_plugins = ["bedrock.testing"]

That makes every fixture below available: ``bedrock_runtime`` (an isolated module registry that
``bedrock.setup()`` acts on), ``module_factory`` (temporary Bedrock modules), ``db_session``
(rollback-isolated database session), and the DI and hook fixtures. The context managers
``isolated_runtime()``, ``rollback_session()`` and ``arollback_session()`` back those fixtures and can
be used directly, for example inside an async fixture.
"""

from .database import arollback_session, db_session, rollback_session
from .di import clean_container, di_container, override_service
from .hooks import clean_hooks, hook_namespace, hook_registry
from .modules import ModuleFactory, module_factory
from .runtime import BedrockRuntime, LifecycleEvent, bedrock_runtime, isolated_runtime

__all__ = [
    "BedrockRuntime",
    "LifecycleEvent",
    "ModuleFactory",
    "arollback_session",
    "bedrock_runtime",
    "clean_container",
    "clean_hooks",
    "db_session",
    "di_container",
    "hook_namespace",
    "hook_registry",
    "isolated_runtime",
    "module_factory",
    "override_service",
    "rollback_session",
]
