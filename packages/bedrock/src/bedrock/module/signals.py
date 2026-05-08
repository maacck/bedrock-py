"""Named Bedrock module lifecycle signals."""

from __future__ import annotations

from bedrock.signal import Namespace

module_signals = Namespace()

module_loaded = module_signals.signal("bedrock.module.on_load")
"""Emitted after a module has been loaded and its on_load hook called."""

module_ready = module_signals.signal("bedrock.module.ready")
"""Emitted after all modules are loaded and each module's ready hook called."""

module_shutdown = module_signals.signal("bedrock.module.on_shutdown")
"""Emitted during shutdown in reverse dependency order."""

module_installed = module_signals.signal("bedrock.module.installed")

registry_ready = module_signals.signal("bedrock.registry.ready")
"""Emitted after all modules have been marked ready."""

registry_shutdown = module_signals.signal("bedrock.registry.shutdown")
"""Emitted when the registry begins shutdown."""

__all__ = [
    "module_loaded",
    "module_ready",
    "module_shutdown",
    "registry_ready",
    "registry_shutdown",
]
