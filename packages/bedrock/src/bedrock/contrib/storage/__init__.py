"""Bedrock Storage — file storage abstraction with pluggable backends."""

from .backends.local import LocalBackend, LocalStorageSettings
from .backends.s3 import S3Backend, S3StorageSettings
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
    # Exceptions
    "StorageBackendNotConfiguredError",
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
    # Singleton
    "storage",
]
