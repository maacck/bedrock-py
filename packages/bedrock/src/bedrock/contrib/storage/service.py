"""Storage service: singleton facade over pluggable backends."""

from pathlib import Path
from typing import BinaryIO, Iterable
from urllib.parse import urlparse, urlunparse

from pydantic_settings import BaseSettings

from bedrock.common.registry import ClassRegistry

from .base import StorageBackend
from .entities import StorageListResult, StorageObject, StorageUploadResult
from .exc import StorageBackendNotConfiguredError, StorageError, StorageKeyError

_BACKEND_REGISTRY = ClassRegistry(
    {
        "local": "bedrock.contrib.storage.backends.local:LocalBackend",
        "s3": "bedrock.contrib.storage.backends.s3:S3Backend",
    }
)


def register_backend(name: str, backend_cls: type[StorageBackend]) -> None:
    """Register a custom backend class under ``name`` for :meth:`StorageService.configure`.

    Args:
        name: Unique backend identifier (e.g. ``"stub"``).
        backend_cls: The backend class implementing the :class:`StorageBackend` protocol.
    """
    _BACKEND_REGISTRY.register(name, backend_cls)


def list_backends() -> list[str]:
    """Return all registered backend names."""
    return sorted(_BACKEND_REGISTRY.keys())


def normalize_storage_key(key: str) -> str:
    """Normalize a storage key to POSIX style.

    Strips leading/trailing slashes and whitespace, collapses duplicate
    separators, and rejects empty results and ``..`` path segments.

    Args:
        key: Raw storage key.

    Returns:
        Normalized key.

    Raises:
        StorageKeyError: If the key is empty or escapes its root via ``..``.
    """
    if not isinstance(key, str):
        raise StorageKeyError(msg="storage_key must be a string.")
    normalized = key.strip().strip("/")
    if not normalized:
        raise StorageKeyError(msg="storage_key must not be empty.")
    parts = [part for part in normalized.split("/") if part]
    if ".." in parts:
        raise StorageKeyError(msg="storage_key must not contain '..' segments.")
    return "/".join(parts)


class StorageService:
    """Unified storage service with dynamic backend loading.

    Provides a single entry point for all storage operations. The backend
    must be configured explicitly before use::

        from bedrock.contrib.storage import storage

        storage.configure("local")  # or "s3", with settings

        storage.upload("a/b.txt", b"hello")

    Calling an operation before :meth:`configure` raises
    :class:`StorageBackendNotConfiguredError`.
    """

    _backend: StorageBackend | None
    _cdn_base_url: str | None

    def __init__(self) -> None:
        self._backend = None
        self._cdn_base_url = None

    def configure(
        self,
        backend_name: str = "local",
        settings: BaseSettings | None = None,
        cdn_base_url: str | None = None,
    ) -> StorageBackend:
        """Configure the storage service with a specific backend.

        Args:
            backend_name: Backend identifier (``"local"``, ``"s3"``).
            settings: Optional settings instance. If ``None``, the backend's
                default settings (env-driven) are used.
            cdn_base_url: Optional CDN origin (e.g. ``"https://cdn.example.com"``).
                When set, the hosts of URLs from :meth:`get_access_url` and
                :meth:`get_preview_url` are rewritten to it (query params kept).

        Returns:
            The newly configured backend instance.

        Raises:
            StorageBackendNotConfiguredError: If the backend is not available.
            StorageConnectionError: If the backend cannot be initialized (e.g. missing bucket).
        """
        if not _BACKEND_REGISTRY.has(name=backend_name):
            available = ", ".join(sorted(_BACKEND_REGISTRY.keys()))
            raise StorageBackendNotConfiguredError(
                f"Unknown storage backend '{backend_name}'. Available: {available}. "
                f"Install optional dependencies for additional backends."
            )

        backend_cls = _BACKEND_REGISTRY.get(backend_name)
        if backend_cls is None:
            raise StorageBackendNotConfiguredError(f"Failed to load backend '{backend_name}'.")

        self._backend = backend_cls(settings=settings)
        self._cdn_base_url = cdn_base_url
        return self._backend

    def get_backend(self) -> StorageBackend:
        """Return the current backend.

        Raises:
            StorageBackendNotConfiguredError: If no backend has been configured.
        """
        if self._backend is None:
            raise StorageBackendNotConfiguredError(
                "Storage backend is not configured. Call storage.configure('local') "
                "or storage.configure('s3', settings=...)."
            )
        return self._backend

    def list_backends(self) -> list[str]:
        """Return all registered backend names."""
        return sorted(_BACKEND_REGISTRY.keys())

    def close(self) -> None:
        """Close backend connections and clear the CDN configuration."""
        if self._backend is not None:
            self._backend.close()
            self._backend = None
        self._cdn_base_url = None

    def upload(
        self,
        storage_key: str,
        data: bytes | Path | BinaryIO,
        mime_type: str | None = None,
        provider_metadata: dict[str, str] | None = None,
        acl: str | None = None,
    ) -> StorageUploadResult:
        """Upload ``data`` (bytes, path, or file-like) to ``storage_key``.

        ``acl`` is passed through to backends that support it (S3 canned ACLs);
        others ignore it.
        """
        key = normalize_storage_key(storage_key)
        return self.get_backend().upload(
            key, data, mime_type=mime_type, provider_metadata=provider_metadata, acl=acl
        )

    def download(self, storage_key: str) -> bytes:
        """Return the full object content as bytes."""
        key = normalize_storage_key(storage_key)
        return self.get_backend().download(key)

    def stream(self, storage_key: str, chunk_size: int = 1_048_576) -> Iterable[bytes]:
        """Yield the object content in chunks for large files."""
        key = normalize_storage_key(storage_key)
        return self.get_backend().stream(key, chunk_size=chunk_size)

    def move(self, storage_key: str, dest_storage_key: str) -> None:
        """Move an object to a new key within the same backend."""
        key = normalize_storage_key(storage_key)
        dest = normalize_storage_key(dest_storage_key)
        return self.get_backend().move(key, dest)

    def copy(self, storage_key: str, dest_storage_key: str) -> None:
        """Copy an object to a new key within the same backend."""
        key = normalize_storage_key(storage_key)
        dest = normalize_storage_key(dest_storage_key)
        return self.get_backend().copy(key, dest)

    def list(
        self,
        prefix: str | None = None,
        limit: int | None = None,
        continuation_token: str | None = None,
    ) -> StorageListResult:
        """List the immediate children of a directory prefix (top level when ``prefix`` is None).

        The prefix is normalized and treated as a directory: ``"a"`` and
        ``"a/"`` are equivalent and never match objects under ``a2/``.
        """
        if prefix is None:
            normalized_prefix = None
        else:
            normalized_prefix = normalize_storage_key(prefix)
            if not normalized_prefix.endswith("/"):
                normalized_prefix += "/"
        return self.get_backend().list(normalized_prefix, limit=limit, continuation_token=continuation_token)

    def head(self, storage_key: str) -> StorageObject | None:
        """Return object metadata, or ``None`` when the object does not exist."""
        key = normalize_storage_key(storage_key)
        return self.get_backend().head(key)

    def exists(self, storage_key: str) -> bool:
        """Return ``True`` when the object exists."""
        key = normalize_storage_key(storage_key)
        return self.get_backend().exists(key)

    def delete(self, storage_key: str) -> bool:
        """Delete an object; return ``True`` when it existed."""
        key = normalize_storage_key(storage_key)
        return self.get_backend().delete(key)

    def get_signed_url(self, storage_key: str, method: str = "GET", expires_in: int = 3600) -> str:
        """Return a time-limited URL for ``method`` (``GET``/``PUT``; ``POST`` form uploads are not supported in v1).

        Never rewritten by CDN; backends without support raise
        StorageUrlUnsupportedError.
        """
        self._validate_expires_in(expires_in)
        key = normalize_storage_key(storage_key)
        return self.get_backend().get_signed_url(key, method=method, expires_in=expires_in)

    def get_access_url(self, storage_key: str, acl: str | None = None, expires_in: int = 3600) -> str:
        """Return a direct access (download) URL, rewritten through the CDN when configured.

        Signed unless the resolved ACL is public (``public-read`` /
        ``public-read-write``).
        """
        self._validate_expires_in(expires_in)
        key = normalize_storage_key(storage_key)
        url = self.get_backend().get_access_url(key, acl=acl, expires_in=expires_in)
        return self._rewrite_with_cdn(url)

    def get_preview_url(self, storage_key: str, acl: str | None = None, expires_in: int = 3600) -> str:
        """Return an inline preview URL, rewritten through the CDN when configured.

        Signed unless the resolved ACL is public.
        """
        self._validate_expires_in(expires_in)
        key = normalize_storage_key(storage_key)
        url = self.get_backend().get_preview_url(key, acl=acl, expires_in=expires_in)
        return self._rewrite_with_cdn(url)

    @staticmethod
    def _validate_expires_in(expires_in: int) -> None:
        """Reject non-positive, non-int, or bool ``expires_in`` values before delegation."""
        if not isinstance(expires_in, int) or isinstance(expires_in, bool) or expires_in <= 0:
            raise StorageError(msg=f"expires_in must be a positive integer, got {expires_in!r}.")

    def _rewrite_with_cdn(self, url: str) -> str:
        """Replace the URL host with the configured CDN origin, keeping path and query."""
        if not self._cdn_base_url:
            return url
        parsed = urlparse(url)
        cdn = urlparse(self._cdn_base_url)
        return urlunparse(
            (
                cdn.scheme or parsed.scheme,
                cdn.netloc or parsed.netloc,
                parsed.path,
                parsed.params,
                parsed.query,
                parsed.fragment,
            )
        )


storage = StorageService()
