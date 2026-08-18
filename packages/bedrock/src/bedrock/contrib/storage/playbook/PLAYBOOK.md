# Bedrock Storage

Unified file/object storage with pluggable backends: a local filesystem backend (atomic writes, path-traversal containment) and an S3-compatible backend (canned ACLs, SigV4 presigned URLs, CDN rewriting). Configure a backend before use.

## Quick Reference

| Concern | Import |
|---------|--------|
| Storage singleton | `from bedrock.contrib.storage import storage` |
| Register a backend | `from bedrock.contrib.storage import register_backend` |
| List backends | `from bedrock.contrib.storage import list_backends` |
| Normalize a key | `from bedrock.contrib.storage import normalize_storage_key` |

## Setup

```python
from bedrock.contrib.storage import storage

storage.configure("local")                                # default
storage.configure("s3", settings=S3StorageSettings(bucket_name="my-bucket"))  # storage-s3 extra
```

## Key Operations

- `upload(key, data, mime_type=None, provider_metadata=None, acl=None)` — `data` is `bytes`, `Path`, or a file-like.
- `download(key)` / `stream(key, chunk_size=...)`.
- `move(key, dest)` / `copy(key, dest)` / `delete(key)` / `exists(key)` / `head(key)`.
- `list(prefix=None, limit=None, continuation_token=None)` — immediate children; cursor-paginated.
- `get_signed_url(key, method="GET", expires_in=3600)` — GET/PUT only.
- `get_access_url(key, acl=None)` / `get_preview_url(key, acl=None)` — unsigned for public ACLs, signed otherwise; rewritten through the CDN when configured.

Every key is normalized: leading/trailing `/` stripped and `..` segments rejected (`StorageKeyError`).

## Backends

| Backend | Name | Extra | Env prefix |
|---------|------|-------|------------|
| Local filesystem | `"local"` | none | `STORAGE_LOCAL_` |
| S3-compatible | `"s3"` | `storage-s3` | `STORAGE_S3_` |

### Local

Settings: `base_dir` (`./storage`), `create_dir` (`true`). Writes are atomic (tmp file + `os.replace`); mime/metadata persist in a `<key>.bmeta.json` sidecar. `_path` enforces containment via `Path.resolve()` + `is_relative_to()`, so keys and symlinks cannot escape the root. URL operations raise `StorageUrlUnsupportedError`.

### S3

Settings: `bucket_name` (required), `region_name`, `access_key_id`, `secret_access_key`, `session_token`, `endpoint_url`, `default_acl`, `cdn_base_url`. Explicit ACLs are validated against AWS canned ACLs; presigned URLs use SigV4.

## Exceptions

All inherit from `StorageError` (a `BedrockExc`): `StorageBackendNotConfiguredError`, `StorageConnectionError`, `StorageObjectNotFoundError`, `StorageKeyError`, `StorageUploadError`, `StorageDownloadError`, `StoragePermissionError`, `StorageUrlUnsupportedError`.

## Anti-Patterns

- Don't call operations before `configure()` — they raise `StorageBackendNotConfiguredError`.
- Don't build URLs from the local backend — it has no URL operations.
- Don't rely on unsigned URLs for private objects — use `get_signed_url`.

## See Also

- Guide: `docs-web/content/docs/en/(bedrock)/guides/storage.mdx`
