"""Hook system — structured call/response protocol for module extension points."""

from .exc import HookCallError, HookError, HookSpecNotFoundError, HookValidationError
from .markers import hookimpl, hookspec
from .namespace import HookNamespace
from .registry import HookRegistry

hooks = HookRegistry()

__all__ = [
    "HookCallError",
    "HookError",
    "HookNamespace",
    "HookRegistry",
    "HookSpecNotFoundError",
    "HookValidationError",
    "hookimpl",
    "hooks",
    "hookspec",
]
