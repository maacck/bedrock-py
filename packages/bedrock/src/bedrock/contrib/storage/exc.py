"""Storage module exceptions."""

from bedrock.exc import BedrockExc


class StorageError(BedrockExc):
    """Base exception for the storage contrib module."""

    detail: str = "Storage operation failed."


class StorageBackendNotConfiguredError(StorageError):
    """Raised when no backend is configured or the backend name is unknown."""

    detail: str = "Storage backend is not configured."


class StorageConnectionError(StorageError):
    """Raised when the storage backend cannot be reached or is misconfigured."""

    detail: str = "Failed to connect to the storage backend."


class StorageObjectNotFoundError(StorageError):
    """Raised when a storage object does not exist."""

    detail: str = "The storage object was not found."


class StorageKeyError(StorageError):
    """Raised when a storage key is invalid (empty, escaped, or malformed)."""

    detail: str = "Invalid storage key."


class StorageUploadError(StorageError):
    """Raised when an upload or move operation fails."""

    detail: str = "Failed to upload the storage object."


class StorageDownloadError(StorageError):
    """Raised when a download or stream operation fails."""

    detail: str = "Failed to download the storage object."


class StoragePermissionError(StorageError):
    """Raised when the backend denies access to an object or bucket."""

    detail: str = "Insufficient permissions for the storage operation."


class StorageUrlUnsupportedError(StorageError):
    """Raised when the backend does not support URL generation."""

    detail: str = "The storage backend does not support URL generation."
