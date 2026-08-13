"""S3-compatible storage backend."""

import mimetypes
from pathlib import Path
from typing import BinaryIO, Iterable

from pydantic_settings import BaseSettings, SettingsConfigDict

from ..entities import StorageListEntry, StorageListResult, StorageObject, StorageUploadResult
from ..exc import (
    StorageConnectionError,
    StorageDownloadError,
    StorageError,
    StorageObjectNotFoundError,
    StoragePermissionError,
    StorageUploadError,
)

_S3_CANNED_ACLS = frozenset(
    {
        "private",
        "public-read",
        "public-read-write",
        "authenticated-read",
        "aws-exec-read",
        "bucket-owner-read",
        "bucket-owner-full-control",
    }
)

#: ACLs whose objects are anonymously readable — the only ones that yield unsigned URLs.
_PUBLIC_ACLS = frozenset({"public-read", "public-read-write"})

_METHOD_TO_CLIENT = {"GET": "get_object", "PUT": "put_object"}


class S3StorageSettings(BaseSettings):
    """Settings for the S3 backend (works with any S3-compatible endpoint)."""

    model_config = SettingsConfigDict(env_prefix="STORAGE_S3_", extra="ignore")

    bucket_name: str = ""
    region_name: str = "us-east-1"
    access_key_id: str | None = None
    secret_access_key: str | None = None
    session_token: str | None = None
    endpoint_url: str | None = None
    default_acl: str | None = None


class S3Backend:
    """Store objects in an S3-compatible bucket using boto3."""

    def __init__(self, settings: S3StorageSettings | None = None) -> None:
        self._settings = settings or S3StorageSettings()
        if not self._settings.bucket_name:
            raise StorageConnectionError(
                msg="S3 backend requires bucket_name (set STORAGE_S3_BUCKET_NAME or pass S3StorageSettings)."
            )
        if self._settings.default_acl is not None and self._settings.default_acl not in _S3_CANNED_ACLS:
            allowed = ", ".join(sorted(_S3_CANNED_ACLS))
            raise StorageConnectionError(
                msg=f"Invalid default_acl '{self._settings.default_acl}'. Supported canned ACLs: {allowed}."
            )
        import boto3  # deferred import: boto3 is an optional dependency
        from botocore.config import Config

        self._client = boto3.client(
            "s3",
            region_name=self._settings.region_name,
            aws_access_key_id=self._settings.access_key_id,
            aws_secret_access_key=self._settings.secret_access_key,
            aws_session_token=self._settings.session_token,
            endpoint_url=self._settings.endpoint_url,
            # Explicit SigV4 keeps presigned URLs deterministic (X-Amz-Expires=...) and
            # is required by most S3-compatible endpoints (MinIO, etc.).
            config=Config(signature_version="s3v4"),
        )
        self._bucket = self._settings.bucket_name

    @staticmethod
    def _is_not_found(exc: Exception) -> bool:
        """Return True when the ClientError means the key does not exist."""
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        code = exc.response.get("Error", {}).get("Code", "")
        return status == 404 or code in ("NoSuchKey", "NotFound")

    @staticmethod
    def _validate_acl(acl: str) -> None:
        """Raise StorageError when an explicit ACL is not a canned ACL."""
        if acl not in _S3_CANNED_ACLS:
            allowed = ", ".join(sorted(_S3_CANNED_ACLS))
            raise StorageError(msg=f"Invalid ACL '{acl}'. Supported canned ACLs: {allowed}.")

    def _resolve_acl(self, acl: str | None) -> str | None:
        """Explicit ``acl`` wins over the bucket-level ``default_acl`` from settings."""
        return acl if acl is not None else self._settings.default_acl

    def upload(
        self,
        storage_key: str,
        data: bytes | Path | BinaryIO,
        mime_type: str | None = None,
        provider_metadata: dict[str, str] | None = None,
        acl: str | None = None,
    ) -> StorageUploadResult:
        """Upload ``data`` (bytes, path, or file-like) to ``storage_key``.

        ``acl`` must be an AWS canned ACL; when ``None`` it falls back to the
        bucket-level ``default_acl`` from settings. ``mime_type`` defaults to a
        guess from the key (``mimetypes``), mirroring the local backend.
        """
        resolved_acl = self._resolve_acl(acl)
        if resolved_acl is not None:
            self._validate_acl(resolved_acl)
        kwargs: dict = {}
        content_type = mime_type or mimetypes.guess_type(storage_key)[0]
        if content_type:
            kwargs["ContentType"] = content_type
        if provider_metadata:
            kwargs["Metadata"] = provider_metadata
        if resolved_acl:
            kwargs["ACL"] = resolved_acl
        try:
            if isinstance(data, bytes):
                response = self._client.put_object(Bucket=self._bucket, Key=storage_key, Body=data, **kwargs)
            elif isinstance(data, Path):
                with data.open("rb") as fh:
                    response = self._client.put_object(Bucket=self._bucket, Key=storage_key, Body=fh, **kwargs)
            else:
                response = self._client.put_object(Bucket=self._bucket, Key=storage_key, Body=data, **kwargs)
        except self._client.exceptions.ClientError as exc:
            raise StorageUploadError(msg=f"Failed to upload '{storage_key}': {exc}") from exc
        obj = self.head(storage_key)
        return StorageUploadResult(
            storage_key=storage_key,
            size=obj.size if obj is not None else 0,
            etag=response.get("ETag"),
        )

    def download(self, storage_key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=storage_key)
        except self._client.exceptions.ClientError as exc:
            if self._is_not_found(exc):
                raise StorageObjectNotFoundError(msg=f"Object '{storage_key}' does not exist.") from exc
            raise StorageDownloadError(msg=f"Failed to download '{storage_key}': {exc}") from exc
        return response["Body"].read()

    def stream(self, storage_key: str, chunk_size: int = 1_048_576) -> Iterable[bytes]:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=storage_key)
        except self._client.exceptions.ClientError as exc:
            if self._is_not_found(exc):
                raise StorageObjectNotFoundError(msg=f"Object '{storage_key}' does not exist.") from exc
            raise StorageDownloadError(msg=f"Failed to stream '{storage_key}': {exc}") from exc
        yield from response["Body"].iter_chunks(chunk_size)

    def move(self, storage_key: str, dest_storage_key: str) -> None:
        self.copy(storage_key, dest_storage_key)
        self.delete(storage_key)

    def copy(self, storage_key: str, dest_storage_key: str) -> None:
        try:
            self._client.copy_object(
                Bucket=self._bucket,
                Key=dest_storage_key,
                CopySource={"Bucket": self._bucket, "Key": storage_key},
            )
        except self._client.exceptions.ClientError as exc:
            if self._is_not_found(exc):
                raise StorageObjectNotFoundError(msg=f"Object '{storage_key}' does not exist.") from exc
            raise StorageUploadError(msg=f"Failed to copy '{storage_key}': {exc}") from exc

    def list(
        self,
        prefix: str | None = None,
        limit: int | None = None,
        continuation_token: str | None = None,
    ) -> StorageListResult:
        """Return the immediate children of ``prefix`` (top level when ``None``).

        Uses ``Delimiter="/"`` so ``Contents`` yields object entries and
        ``CommonPrefixes`` yields directory entries (trailing-slash key,
        ``is_dir=True``, no size/last_modified). Object and directory entries
        are merged and sorted by ``storage_key``, mirroring the local backend
        and S3's own interleaved lexicographic ordering.
        """
        kwargs: dict = {"Bucket": self._bucket, "Delimiter": "/"}
        if prefix:
            kwargs["Prefix"] = prefix
        if limit is not None:
            kwargs["MaxKeys"] = limit
        if continuation_token:
            kwargs["ContinuationToken"] = continuation_token
        try:
            response = self._client.list_objects_v2(**kwargs)
        except self._client.exceptions.ClientError as exc:
            raise StoragePermissionError(msg=f"Failed to list objects: {exc}") from exc
        entries = [
            StorageListEntry(
                storage_key=entry["Key"],
                size=entry.get("Size"),
                last_modified=entry.get("LastModified"),
            )
            for entry in response.get("Contents", [])
        ]
        entries.extend(
            StorageListEntry(storage_key=prefix_entry["Prefix"], is_dir=True)
            for prefix_entry in response.get("CommonPrefixes", [])
        )
        entries.sort(key=lambda entry: entry.storage_key)
        return StorageListResult(
            items=entries,
            continuation_token=response.get("NextContinuationToken"),
            truncated=response.get("IsTruncated", False),
        )

    def head(self, storage_key: str) -> StorageObject | None:
        try:
            response = self._client.head_object(Bucket=self._bucket, Key=storage_key)
        except self._client.exceptions.ClientError as exc:
            if self._is_not_found(exc):
                return None
            raise StoragePermissionError(msg=f"Failed to head '{storage_key}': {exc}") from exc
        return StorageObject(
            storage_key=storage_key,
            size=response.get("ContentLength", 0),
            etag=response.get("ETag"),
            last_modified=response.get("LastModified"),
            mime_type=response.get("ContentType"),
            provider_metadata=response.get("Metadata", {}),
        )

    def exists(self, storage_key: str) -> bool:
        """Return ``True`` when the object exists."""
        return self.head(storage_key) is not None

    def delete(self, storage_key: str) -> bool:
        exists = self.head(storage_key) is not None
        if not exists:
            return False
        self._client.delete_object(Bucket=self._bucket, Key=storage_key)
        return True

    def get_signed_url(self, storage_key: str, method: str = "GET", expires_in: int = 3600) -> str:
        """Return a presigned URL for ``method`` (``GET`` or ``PUT`` only).

        Presigned POST uploads are unsupported here: boto3 exposes no
        ``post_object`` client method — presigned POST uploads use the multipart
        form protocol (``generate_presigned_post`` returns ``{url, fields}``),
        which a single-URL API cannot express. Use :meth:`upload` instead, or a
        future presigned-post API.
        """
        client_method = _METHOD_TO_CLIENT.get(method.upper())
        if client_method is None:
            raise StorageError(
                msg=f"Unsupported signed URL method '{method}'. Supported: GET, PUT. "
                "Presigned POST uploads need the form protocol (url + fields) which "
                "get_signed_url cannot express in v1; use upload() instead, or a "
                "future presigned-post API."
            )
        try:
            return self._client.generate_presigned_url(
                client_method,
                Params={"Bucket": self._bucket, "Key": storage_key},
                ExpiresIn=expires_in,
            )
        except Exception as exc:  # noqa: BLE001 - surface boto3 client limitations as StorageError
            raise StorageError(msg=f"Failed to presign '{method}' for '{storage_key}': {exc}") from exc

    def get_access_url(self, storage_key: str, acl: str | None = None, expires_in: int = 3600) -> str:
        """Return a direct access URL: unsigned for public ACLs, signed otherwise."""
        resolved_acl = self._resolve_acl(acl)
        if resolved_acl is not None:
            self._validate_acl(resolved_acl)
        if resolved_acl in _PUBLIC_ACLS:
            return self._plain_url(storage_key)
        return self.get_signed_url(storage_key, method="GET", expires_in=expires_in)

    def get_preview_url(self, storage_key: str, acl: str | None = None, expires_in: int = 3600) -> str:
        """Return an inline preview URL (browser-safe, ``ResponseContentDisposition=inline``).

        Unsigned for public ACLs, signed otherwise.
        """
        resolved_acl = self._resolve_acl(acl)
        if resolved_acl is not None:
            self._validate_acl(resolved_acl)
        if resolved_acl in _PUBLIC_ACLS:
            return f"{self._plain_url(storage_key)}?response-content-disposition=inline"
        return self._client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self._bucket,
                "Key": storage_key,
                "ResponseContentDisposition": "inline",
            },
            ExpiresIn=expires_in,
        )

    def _plain_url(self, storage_key: str) -> str:
        """Unsigned path-style URL against the configured endpoint."""
        endpoint = self._client.meta.endpoint_url.rstrip("/")
        return f"{endpoint}/{self._bucket}/{storage_key}"

    def close(self) -> None:
        """Close the underlying boto3 client."""
        self._client.close()
