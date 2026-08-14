"""Local filesystem storage backend."""

import mimetypes
import os
import shutil
import stat
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

import orjson
from pydantic_settings import BaseSettings, SettingsConfigDict

from ..entities import StorageListEntry, StorageListResult, StorageObject, StorageUploadResult
from ..exc import (
    StorageDownloadError,
    StorageKeyError,
    StorageObjectNotFoundError,
    StoragePermissionError,
    StorageUploadError,
    StorageUrlUnsupportedError,
)

_METADATA_SUFFIX = ".bmeta.json"

_URL_UNSUPPORTED_MESSAGE = "The local backend does not support URL generation; build URLs in your application instead."


class LocalStorageSettings(BaseSettings):
    """Settings for the local filesystem backend."""

    model_config = SettingsConfigDict(env_prefix="STORAGE_LOCAL_", extra="ignore")

    base_dir: str = "./storage"
    create_dir: bool = True


class LocalBackend:
    """Store objects as files under a base directory.

    Writes are atomic (tmp file + ``os.replace``). Optional mime_type and
    metadata persist in a sidecar file ``<key>.bmeta.json``; ``list()``
    excludes sidecars and ``move``/``copy``/``delete`` carry them along.
    """

    def __init__(self, settings: LocalStorageSettings | None = None) -> None:
        self._settings = settings or LocalStorageSettings()
        self._root = Path(self._settings.base_dir)
        if self._settings.create_dir:
            self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, storage_key: str) -> Path:
        """Return the absolute on-disk path for ``storage_key``, enforcing containment.

        Raises:
            StorageKeyError: If the key resolves outside the backend root.
        """
        root = self._root.resolve()
        resolved = (self._root / storage_key).resolve()
        if not resolved.is_relative_to(root):
            raise StorageKeyError(msg=f"storage_key {storage_key!r} resolves outside the backend root {str(root)!r}.")
        return resolved

    def _meta_path(self, storage_key: str) -> Path:
        return Path(str(self._path(storage_key)) + _METADATA_SUFFIX)

    def _write_meta(self, storage_key: str, mime_type: str | None, provider_metadata: dict[str, str] | None) -> None:
        meta_path = self._meta_path(storage_key)
        if mime_type is None and not provider_metadata:
            meta_path.unlink(missing_ok=True)
            return
        payload = {
            "mime_type": mime_type or mimetypes.guess_type(storage_key)[0],
            "provider_metadata": provider_metadata or {},
        }
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(orjson.dumps(payload).decode("utf-8"))

    def _read_meta(self, storage_key: str) -> dict | None:
        meta_path = self._meta_path(storage_key)
        if not meta_path.exists():
            return None
        return orjson.loads(meta_path.read_bytes())

    def upload(
        self,
        storage_key: str,
        data: bytes | Path | BinaryIO,
        mime_type: str | None = None,
        provider_metadata: dict[str, str] | None = None,
        acl: str | None = None,
    ) -> StorageUploadResult:
        """Upload ``data`` (bytes, path, or file-like) to ``storage_key``.

        ``acl`` is accepted for API symmetry but has no effect on the local
        filesystem.
        """
        dest = self._path(storage_key)
        tmp: Path | None = None
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.parent / f".{dest.name}.tmp-{uuid.uuid4().hex}"
            with tmp.open("wb") as fh:
                if isinstance(data, bytes):
                    fh.write(data)
                elif isinstance(data, Path):
                    with data.open("rb") as src:
                        shutil.copyfileobj(src, fh)
                else:
                    shutil.copyfileobj(data, fh)
            os.replace(tmp, dest)
            self._write_meta(storage_key, mime_type, provider_metadata)
        except StorageKeyError:
            raise
        except OSError as exc:
            if tmp is not None:
                try:
                    tmp.unlink(missing_ok=True)  # best-effort cleanup of the partial write
                except OSError:
                    pass  # the original failure is authoritative
            raise StorageUploadError(msg=f"Failed to upload {storage_key!r}: {exc}") from exc
        return StorageUploadResult(storage_key=storage_key, size=dest.stat().st_size)

    def download(self, storage_key: str) -> bytes:
        """Return the full object content as bytes."""
        path = self._path(storage_key)
        try:
            stat_result = path.stat()
        except FileNotFoundError as exc:
            raise StorageObjectNotFoundError(msg=f"Object {storage_key!r} does not exist.") from exc
        except OSError as exc:
            raise StorageDownloadError(msg=f"Failed to download {storage_key!r}: {exc}") from exc
        if not stat.S_ISREG(stat_result.st_mode):
            raise StorageObjectNotFoundError(msg=f"Object {storage_key!r} does not exist.")
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise StorageObjectNotFoundError(msg=f"Object {storage_key!r} does not exist.") from exc
        except OSError as exc:
            raise StorageDownloadError(msg=f"Failed to download {storage_key!r}: {exc}") from exc

    def stream(self, storage_key: str, chunk_size: int = 1_048_576) -> Iterable[bytes]:
        """Yield the object content in chunks."""
        path = self._path(storage_key)
        try:
            stat_result = path.stat()
        except FileNotFoundError as exc:
            raise StorageObjectNotFoundError(msg=f"Object {storage_key!r} does not exist.") from exc
        except OSError as exc:
            raise StorageDownloadError(msg=f"Failed to stream {storage_key!r}: {exc}") from exc
        if not stat.S_ISREG(stat_result.st_mode):
            raise StorageObjectNotFoundError(msg=f"Object {storage_key!r} does not exist.")
        try:
            with path.open("rb") as fh:
                while True:
                    chunk = fh.read(chunk_size)
                    if not chunk:
                        return
                    yield chunk
        except FileNotFoundError as exc:
            raise StorageObjectNotFoundError(msg=f"Object {storage_key!r} does not exist.") from exc
        except OSError as exc:
            raise StorageDownloadError(msg=f"Failed to stream {storage_key!r}: {exc}") from exc

    def move(self, storage_key: str, dest_storage_key: str) -> None:
        """Move an object (and its metadata sidecar) to a new key."""
        src = self._path(storage_key)
        dest = self._path(dest_storage_key)
        try:
            if not stat.S_ISREG(src.stat().st_mode):
                raise StorageObjectNotFoundError(msg=f"Object {storage_key!r} does not exist.")
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, dest)
        except FileNotFoundError as exc:
            raise StorageObjectNotFoundError(msg=f"Object {storage_key!r} does not exist.") from exc
        except OSError as exc:
            raise StorageUploadError(msg=f"Failed to move {storage_key!r} to {dest_storage_key!r}: {exc}") from exc
        try:
            self._move_sidecar(storage_key, dest_storage_key)
        except OSError as exc:
            raise StorageUploadError(msg=f"Failed to move metadata for {storage_key!r}: {exc}") from exc

    def copy(self, storage_key: str, dest_storage_key: str) -> None:
        """Copy an object (and its metadata sidecar) to a new key."""
        src = self._path(storage_key)
        dest = self._path(dest_storage_key)
        try:
            if not stat.S_ISREG(src.stat().st_mode):
                raise StorageObjectNotFoundError(msg=f"Object {storage_key!r} does not exist.")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
        except FileNotFoundError as exc:
            raise StorageObjectNotFoundError(msg=f"Object {storage_key!r} does not exist.") from exc
        except OSError as exc:
            raise StorageUploadError(msg=f"Failed to copy {storage_key!r} to {dest_storage_key!r}: {exc}") from exc
        try:
            self._copy_sidecar(storage_key, dest_storage_key)
        except OSError as exc:
            raise StorageUploadError(msg=f"Failed to copy metadata for {storage_key!r}: {exc}") from exc

    def list(
        self,
        prefix: str | None = None,
        limit: int | None = None,
        continuation_token: str | None = None,
    ) -> StorageListResult:
        """Return the immediate children of ``prefix`` (top level when None).

        The service passes ``prefix`` already normalized with a trailing slash
        (or ``None`` for the top level). Directory entries use a trailing-slash
        key with ``is_dir=True`` and no size/last_modified, and are emitted
        only when the subtree contains at least one object file (S3
        common-prefix semantics; empty or sidecar-only directories are
        omitted). Object entries carry size and UTC ``last_modified`` (same
        provenance as ``head``). Metadata sidecars are never listed.
        Pagination: ``continuation_token`` is the last returned entry's key
        and entries whose key sorts at or below it are skipped.
        """
        prefix = prefix or ""
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        scan_dir = self._root if not prefix else self._path(prefix)
        if not scan_dir.is_dir():
            return StorageListResult(items=[])
        entries: list[StorageListEntry] = []
        try:
            for child in scan_dir.iterdir():
                name = child.name
                if name.endswith(_METADATA_SUFFIX):
                    continue
                if child.is_dir():
                    if self._dir_has_objects(child):
                        entries.append(StorageListEntry(storage_key=f"{prefix}{name}/", is_dir=True))
                else:
                    try:
                        stat_result = child.stat()
                    except FileNotFoundError:
                        continue  # object vanished between scan and stat
                    except OSError as exc:
                        raise StoragePermissionError(
                            msg=f"Failed to stat {name!r} while listing {prefix!r}: {exc}"
                        ) from exc
                    entries.append(
                        StorageListEntry(
                            storage_key=f"{prefix}{name}",
                            size=stat_result.st_size,
                            last_modified=datetime.fromtimestamp(stat_result.st_mtime, tz=UTC),
                        )
                    )
        except OSError as exc:
            raise StoragePermissionError(msg=f"Failed to list {prefix!r}: {exc}") from exc
        entries.sort(key=lambda entry: entry.storage_key)
        if continuation_token:
            entries = [entry for entry in entries if entry.storage_key > continuation_token]
        truncated = False
        token = None
        if limit is not None and len(entries) > limit:
            entries = entries[:limit]
            truncated = True
            token = entries[-1].storage_key
        return StorageListResult(items=entries, continuation_token=token, truncated=truncated)

    def head(self, storage_key: str) -> StorageObject | None:
        """Return object metadata (mime from sidecar, else guessed), or ``None``.

        Directories and other non-regular files are not objects and return
        ``None`` (matching S3, where a directory prefix is never an object).
        """
        path = self._path(storage_key)
        try:
            stat_result = path.stat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StoragePermissionError(msg=f"Failed to access {storage_key!r}: {exc}") from exc
        if not stat.S_ISREG(stat_result.st_mode):
            return None
        meta = self._read_meta(storage_key)
        mime_type = (meta or {}).get("mime_type") or mimetypes.guess_type(storage_key)[0]
        return StorageObject(
            storage_key=storage_key,
            size=stat_result.st_size,
            last_modified=datetime.fromtimestamp(stat_result.st_mtime, tz=UTC),
            mime_type=mime_type,
            provider_metadata=dict((meta or {}).get("provider_metadata") or {}),
        )

    def exists(self, storage_key: str) -> bool:
        """Return ``True`` when the object exists (``head() is not None``)."""
        return self.head(storage_key) is not None

    def delete(self, storage_key: str) -> bool:
        """Delete an object and its metadata sidecar; return ``True`` when it existed.

        Directories and other non-regular files are not objects; deleting
        them reports ``False`` (matching S3, where delete on a non-existent
        object is a no-op).
        """
        path = self._path(storage_key)
        try:
            stat_result = path.stat()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise StoragePermissionError(msg=f"Failed to delete {storage_key!r}: {exc}") from exc
        if not stat.S_ISREG(stat_result.st_mode):
            return False
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except IsADirectoryError:
            return False
        except OSError as exc:
            raise StoragePermissionError(msg=f"Failed to delete {storage_key!r}: {exc}") from exc
        try:
            self._delete_sidecar(storage_key)
        except OSError as exc:
            raise StoragePermissionError(msg=f"Failed to delete metadata for {storage_key!r}: {exc}") from exc
        return True

    def get_signed_url(self, storage_key: str, method: str = "GET", expires_in: int = 3600) -> str:
        """Raise StorageUrlUnsupportedError: the local backend has no URL operations."""
        raise StorageUrlUnsupportedError(msg=_URL_UNSUPPORTED_MESSAGE)

    def get_access_url(self, storage_key: str, acl: str | None = None, expires_in: int = 3600) -> str:
        """Raise StorageUrlUnsupportedError: the local backend has no URL operations."""
        raise StorageUrlUnsupportedError(msg=_URL_UNSUPPORTED_MESSAGE)

    def get_preview_url(self, storage_key: str, acl: str | None = None, expires_in: int = 3600) -> str:
        """Raise StorageUrlUnsupportedError: the local backend has no URL operations."""
        raise StorageUrlUnsupportedError(msg=_URL_UNSUPPORTED_MESSAGE)

    def close(self) -> None:
        """Release backend resources (none for the local filesystem)."""

    def _dir_has_objects(self, path: Path) -> bool:
        """Return ``True`` when the subtree at ``path`` holds at least one object file.

        Metadata sidecars (``*.bmeta.json``) do not count as objects.
        Directory symlinks are never traversed and symlinked entries are
        skipped entirely, so a symlink escaping the backend root cannot leak
        its target's content into ``list()``; every counted file must also
        resolve inside the root (the same containment ``_path`` enforces).
        Short-circuits on the first regular file; subtrees that cannot be
        walked are treated as empty so unreadable or vanishing directories are
        simply not emitted.
        """
        root = self._root.resolve()
        try:
            pending = [path]
            while pending:
                current = pending.pop()
                if current.is_symlink():
                    continue
                for child in current.iterdir():
                    if child.is_symlink():
                        continue
                    if child.is_dir():
                        pending.append(child)
                    elif child.is_file() and not child.name.endswith(_METADATA_SUFFIX):
                        if child.resolve().is_relative_to(root):
                            return True
        except OSError:
            return False
        return False

    def _move_sidecar(self, storage_key: str, dest_storage_key: str) -> None:
        """Move the metadata sidecar, clearing any stale destination sidecar."""
        src_meta = self._meta_path(storage_key)
        dest_meta = self._meta_path(dest_storage_key)
        if src_meta.exists():
            dest_meta.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src_meta, dest_meta)
        else:
            dest_meta.unlink(missing_ok=True)

    def _copy_sidecar(self, storage_key: str, dest_storage_key: str) -> None:
        """Copy the metadata sidecar, clearing any stale destination sidecar."""
        src_meta = self._meta_path(storage_key)
        dest_meta = self._meta_path(dest_storage_key)
        if src_meta.exists():
            dest_meta.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src_meta, dest_meta)
        else:
            dest_meta.unlink(missing_ok=True)

    def _delete_sidecar(self, storage_key: str) -> None:
        self._meta_path(storage_key).unlink(missing_ok=True)
