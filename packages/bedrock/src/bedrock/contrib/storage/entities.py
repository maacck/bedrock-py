"""Storage domain entities."""

from datetime import datetime

from pydantic import Field

from bedrock.entities import BedrockEntity


class StorageObject(BedrockEntity):
    """Metadata describing a stored object (result of :meth:`head`).

    ``last_modified`` is always timezone-aware UTC. ``provider_metadata`` is
    an opaque, backend-specific string dict; ``mime_type`` is a MIME type.
    """

    storage_key: str
    size: int
    etag: str | None = None
    last_modified: datetime | None = None
    mime_type: str | None = None
    provider_metadata: dict[str, str] = Field(default_factory=dict)


class StorageUploadResult(BedrockEntity):
    """Result of an :meth:`upload` operation."""

    storage_key: str
    size: int
    etag: str | None = None


class StoragePresignedUrl(BedrockEntity):
    """Result of :meth:`generate_presigned_url`: a time-limited request the client replays.

    The client sends ``method`` to ``endpoint`` with ``query_params`` as the
    query string and every entry of ``headers`` unchanged: both are part of
    the signature, and the provider rejects the request when one is missing or
    altered. ``query_params`` values are decoded; ``headers`` uses the
    backend's lowercase wire names (e.g. ``x-amz-checksum-sha256`` on S3).
    """

    method: str
    endpoint: str
    query_params: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)


class StorageListEntry(BedrockEntity):
    """A single entry returned by :meth:`list`.

    Object entries carry ``size`` and ``last_modified``; directory entries
    (``is_dir=True``) use a trailing-slash ``storage_key`` and have both
    ``size`` and ``last_modified`` set to ``None``.
    """

    storage_key: str
    size: int | None = None
    last_modified: datetime | None = None
    is_dir: bool = False


class StorageListResult(BedrockEntity):
    """Paged result of a :meth:`list` operation."""

    items: list[StorageListEntry]
    continuation_token: str | None = None
    truncated: bool = False
