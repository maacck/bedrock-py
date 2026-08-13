"""Bedrock Storage — file storage abstraction with pluggable backends."""

from .entities import StorageListEntry, StorageListResult, StorageObject, StorageUploadResult
from .exc import (
    StorageBackendNotConfiguredError,
    StorageConnectionError,
    StorageDownloadError,
    StorageError,
    StorageKeyError,
    StorageObjectNotFoundError,
    StoragePermissionError,
    StorageUploadError,
    StorageUrlUnsupportedError,
)
from .service import StorageService, list_backends, normalize_storage_key, register_backend, storage

__all__ = [
    "StorageBackendNotConfiguredError",
    "StorageConnectionError",
    "StorageDownloadError",
    "StorageError",
    "StorageKeyError",
    "StorageListEntry",
    "StorageListResult",
    "StorageObject",
    "StorageObjectNotFoundError",
    "StoragePermissionError",
    "StorageService",
    "StorageUploadError",
    "StorageUploadResult",
    "StorageUrlUnsupportedError",
    "list_backends",
    "normalize_storage_key",
    "register_backend",
    "storage",
]
