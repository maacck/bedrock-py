"""Bedrock Storage — file storage abstraction with pluggable backends."""

from typing import TYPE_CHECKING

from .entities import StorageListEntry, StorageListResult, StorageObject, StoragePresignedUrl, StorageUploadResult
from .exc import (
    StorageBackendNotConfiguredError,
    StorageCloseError,
    StorageConnectionError,
    StorageDownloadError,
    StorageError,
    StorageKeyError,
    StorageObjectNotFoundError,
    StoragePermissionError,
    StorageUploadError,
    StorageUrlUnsupportedError,
)
from .service import (
    StorageService,
    list_backends,
    normalize_storage_key,
    register_backend,
    register_backend_lazy,
    storage,
)

if TYPE_CHECKING:
    from .backends.local import LocalBackend, LocalStorageSettings
    from .backends.s3 import S3Backend, S3StorageSettings

_LAZY_EXPORTS = {
    "LocalBackend": ".backends.local",
    "LocalStorageSettings": ".backends.local",
    "S3Backend": ".backends.s3",
    "S3StorageSettings": ".backends.s3",
}


def __getattr__(name: str) -> object:
    """Import backend classes on first access so importing the package loads no backend module."""
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(module_name, __name__), name)


__all__ = [
    # Exceptions
    "StorageBackendNotConfiguredError",
    "StorageCloseError",
    "StorageConnectionError",
    "StorageDownloadError",
    "StorageError",
    "StorageKeyError",
    "StorageObjectNotFoundError",
    "StoragePermissionError",
    "StorageUploadError",
    "StorageUrlUnsupportedError",
    # Entities
    "StorageListEntry",
    "StorageListResult",
    "StorageObject",
    "StoragePresignedUrl",
    "StorageUploadResult",
    # Settings and backends
    "LocalBackend",
    "LocalStorageSettings",
    "S3Backend",
    "S3StorageSettings",
    # Service and helpers
    "StorageService",
    "list_backends",
    "normalize_storage_key",
    "register_backend",
    "register_backend_lazy",
    # Singleton
    "storage",
]
