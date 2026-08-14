"""Storage backend protocol (sync-only in v1)."""

from collections.abc import Iterable
from pathlib import Path
from typing import BinaryIO, Protocol

from pydantic_settings import BaseSettings

from .entities import StorageListResult, StorageObject, StorageUploadResult


class StorageBackend(Protocol):
    """Contract every storage backend must implement.

    Backends receive already-normalized ``storage_key`` values; the service
    performs validation and normalization before delegation.
    """

    @property
    def settings(self) -> BaseSettings: ...

    def upload(
        self,
        storage_key: str,
        data: bytes | Path | BinaryIO,
        mime_type: str | None = None,
        provider_metadata: dict[str, str] | None = None,
        acl: str | None = None,
    ) -> StorageUploadResult:
        """Store ``data`` (bytes, path, or file-like) at ``storage_key``.

        ``mime_type`` is the MIME type; ``provider_metadata`` is an opaque
        backend-specific string dict. ``acl`` carries AWS canned ACLs on S3;
        backends without ACL support ignore it.
        """
        ...

    def download(self, storage_key: str) -> bytes:
        """Return the full object content as bytes."""
        ...

    def stream(self, storage_key: str, chunk_size: int = 1_048_576) -> Iterable[bytes]:
        """Yield the object content in chunks."""
        ...

    def move(self, storage_key: str, dest_storage_key: str) -> None:
        """Move an object to a new key within the same backend."""
        ...

    def copy(self, storage_key: str, dest_storage_key: str) -> None:
        """Copy an object to a new key within the same backend."""
        ...

    def list(
        self,
        prefix: str | None = None,
        limit: int | None = None,
        continuation_token: str | None = None,
    ) -> StorageListResult:
        """List objects under an optional prefix, cursor-paginated."""
        ...

    def head(self, storage_key: str) -> StorageObject | None:
        """Return object metadata, or ``None`` when the object does not exist."""
        ...

    def exists(self, storage_key: str) -> bool:
        """Return ``True`` when the object exists (``head() is not None``)."""
        ...

    def delete(self, storage_key: str) -> bool:
        """Delete an object; return ``True`` when it existed."""
        ...

    def get_signed_url(self, storage_key: str, method: str = "GET", expires_in: int = 3600) -> str:
        """Return a time-limited URL for ``method`` (``GET``/``PUT``; ``POST`` form uploads are not supported in v1).

        Backends without support raise StorageUrlUnsupportedError.
        """
        ...

    def get_access_url(self, storage_key: str, acl: str | None = None, expires_in: int = 3600) -> str:
        """Return a direct access (download) URL.

        Signed unless the resolved ACL is public (``public-read`` /
        ``public-read-write``). CDN-rewritten by the service when configured.
        """
        ...

    def get_preview_url(self, storage_key: str, acl: str | None = None, expires_in: int = 3600) -> str:
        """Return an inline preview URL (browser-safe, ``ResponseContentDisposition=inline``).

        Signed unless the resolved ACL is public. CDN-rewritten by the service
        when configured.
        """
        ...

    def close(self) -> None:
        """Release backend resources."""
        ...
