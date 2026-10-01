"""Storage service: singleton facade over pluggable backends."""

import base64
import binascii
import re
import warnings
from collections.abc import Iterable
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote, urlencode, urlparse, urlunparse

from pydantic_settings import BaseSettings

from bedrock.common.registry import ClassRegistry
from bedrock.logging import get_logger

from .base import StorageBackend
from .entities import StorageListResult, StorageObject, StoragePresignedUrl, StorageUploadResult
from .exc import StorageBackendNotConfiguredError, StorageError, StorageKeyError

logger = get_logger(__name__)

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


_PRESIGN_METHODS = frozenset({"GET", "PUT"})

#: RFC 9110 ``token`` characters allowed in a header field name.
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")

#: Headers owned by typed :meth:`StorageService.generate_presigned_url` arguments.
_TYPED_HEADERS = frozenset({"content-type", "content-length"})


def _validate_header_value(label: str, value: object) -> None:
    """Reject non-string values and values that would split or truncate the header line."""
    if not isinstance(value, str):
        raise StorageError(msg=f"{label} must be a string, got {type(value).__name__}.")
    if any(char in value for char in "\r\n\x00"):
        raise StorageError(msg=f"{label} must not contain CR, LF, or NUL characters.")


def _validate_checksum_sha256(value: object) -> None:
    """Require the base64 encoding of a 32-byte SHA-256 digest (the S3 wire format)."""
    if not isinstance(value, str):
        raise StorageError(msg="checksum_sha256 must be a base64 string.")
    try:
        digest = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise StorageError(msg="checksum_sha256 must be valid base64 (not hex).") from exc
    if len(digest) != 32:
        raise StorageError(msg=f"checksum_sha256 must encode a 32-byte SHA-256 digest, got {len(digest)} bytes.")


def _normalize_provider_headers(provider_headers: dict[str, str] | None) -> dict[str, str]:
    """Validate provider headers and return them with lowercased, unique names."""
    if not provider_headers:
        return {}
    normalized: dict[str, str] = {}
    for name, value in provider_headers.items():
        if not isinstance(name, str) or not _HEADER_NAME_RE.match(name):
            raise StorageError(msg=f"Invalid provider header name {name!r}.")
        _validate_header_value(f"provider header {name!r}", value)
        lowered = name.lower()
        if lowered in _TYPED_HEADERS:
            raise StorageError(
                msg=f"Provider header {name!r} is set through a typed argument (mime_type / content_length)."
            )
        if lowered in normalized:
            raise StorageError(msg=f"Duplicate provider header {name!r} (header names are case-insensitive).")
        normalized[lowered] = value
    return normalized


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

    def __init__(self) -> None:
        self._backend = None

    def configure(
        self,
        backend_name: str = "local",
        settings: BaseSettings | None = None,
    ) -> StorageBackend:
        """Configure the storage service with a specific backend.

        Args:
            backend_name: Backend identifier (``"local"``, ``"s3"``).
            settings: Optional settings instance. If ``None``, the backend's
                default settings (env-driven) are used. When the active
                backend's settings carry a CDN origin (``cdn_base_url``, e.g.
                on :class:`~bedrock.contrib.storage.S3StorageSettings`), the
                hosts of URLs from :meth:`get_access_url` and
                :meth:`get_preview_url` are rewritten to it (query params
                kept); :meth:`generate_presigned_url` is never rewritten.

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
        logger.info("Storage backend configured: {}", backend_name)
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
        """Close backend connections and reset the configured backend."""
        if self._backend is not None:
            self._backend.close()
            self._backend = None

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
        return self.get_backend().upload(key, data, mime_type=mime_type, provider_metadata=provider_metadata, acl=acl)

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

    def generate_presigned_url(
        self,
        storage_key: str,
        method: str = "GET",
        expires_in: int = 3600,
        *,
        mime_type: str | None = None,
        content_length: int | None = None,
        checksum_sha256: str | None = None,
        provider_headers: dict[str, str] | None = None,
    ) -> StoragePresignedUrl:
        """Return a presigned request: send ``method`` to ``endpoint`` with ``query_params`` and every ``headers`` entry.

        Upload conditions are signed into the request, so the provider rejects
        an upload whose matching header is missing or differs:

        Args:
            storage_key: Object key (normalized).
            method: ``GET`` or ``PUT``.
            expires_in: Lifetime in seconds (positive int).
            mime_type: ``PUT`` only; signed as ``content-type``.
            content_length: ``PUT`` only; exact body size in bytes, signed as ``content-length``.
            checksum_sha256: ``PUT`` only; base64 of the 32-byte SHA-256 digest of the body,
                mapped to the backend's header (``x-amz-checksum-sha256`` on S3).
            provider_headers: Backend-specific headers signed verbatim (e.g.
                ``{"x-amz-checksum-type": "FULL_OBJECT"}``). Names are returned lowercased.

        Never rewritten by CDN; backends without support raise
        StorageUrlUnsupportedError.

        Raises:
            StorageError: On invalid arguments or a header the backend refuses to sign.
        """
        self._validate_expires_in(expires_in)
        key = normalize_storage_key(storage_key)
        method = method.upper() if isinstance(method, str) else method
        if method not in _PRESIGN_METHODS:
            raise StorageError(msg=f"Unsupported presigned URL method {method!r}. Supported: GET, PUT.")
        has_conditions = mime_type is not None or content_length is not None or checksum_sha256 is not None
        if has_conditions and method != "PUT":
            raise StorageError(msg="mime_type, content_length, and checksum_sha256 can only be signed for PUT.")
        if mime_type is not None:
            _validate_header_value("mime_type", mime_type)
            if not mime_type:
                raise StorageError(msg="mime_type must not be empty.")
        if content_length is not None and (
            not isinstance(content_length, int) or isinstance(content_length, bool) or content_length < 0
        ):
            raise StorageError(msg=f"content_length must be a non-negative integer, got {content_length!r}.")
        if checksum_sha256 is not None:
            _validate_checksum_sha256(checksum_sha256)
        headers = _normalize_provider_headers(provider_headers)
        return self.get_backend().generate_presigned_url(
            key,
            method=method,
            expires_in=expires_in,
            mime_type=mime_type,
            content_length=content_length,
            checksum_sha256=checksum_sha256,
            provider_headers=headers,
        )

    def get_signed_url(self, storage_key: str, method: str = "GET", expires_in: int = 3600) -> str:
        """Deprecated: use :meth:`generate_presigned_url`, which also returns the headers to send.

        Return ``endpoint?query`` of an unconditioned presigned request for
        ``method`` (``GET``/``PUT``). Never rewritten by CDN.
        """
        warnings.warn(
            "storage.get_signed_url() is deprecated; use storage.generate_presigned_url() instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        presigned = self.generate_presigned_url(storage_key, method=method, expires_in=expires_in)
        if not presigned.query_params:
            return presigned.endpoint
        return f"{presigned.endpoint}?{urlencode(presigned.query_params, quote_via=quote, safe='-_.~')}"

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
        """Replace the URL host with the CDN origin from the active backend's settings, keeping path and query."""
        settings = getattr(self.get_backend(), "settings", None)
        cdn_base_url = getattr(settings, "cdn_base_url", None) if settings is not None else None
        if not cdn_base_url:
            return url
        parsed = urlparse(url)
        cdn = urlparse(cdn_base_url)
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
