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
    "LocalBackend",
    "LocalStorageSettings",
    "S3Backend",
    "S3StorageSettings",
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
