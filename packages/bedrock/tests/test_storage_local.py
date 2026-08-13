"""Tests for the local filesystem storage backend."""

import os
from pathlib import Path

import pytest

from bedrock.contrib.storage import (
    LocalStorageSettings,
    StorageKeyError,
    StorageObjectNotFoundError,
    StorageUrlUnsupportedError,
)
from bedrock.contrib.storage.backends.local import LocalBackend
from bedrock.contrib.storage.entities import StorageListEntry
from bedrock.contrib.storage.service import StorageService


@pytest.fixture
def svc(tmp_path: Path) -> StorageService:
    service = StorageService()
    service.configure("local", settings=LocalStorageSettings(base_dir=str(tmp_path)))
    yield service
    service.close()


def test_key_escaping_root_raises(tmp_path: Path) -> None:
    """A key resolving outside the root raises StorageKeyError (backend-level guard)."""
    backend = LocalBackend(settings=LocalStorageSettings(base_dir=str(tmp_path)))
    with pytest.raises(StorageKeyError):
        backend.upload("../escape.txt", b"x")


@pytest.mark.skipif(os.name != "nt", reason="backslash is a path separator only on Windows")
def test_backslash_key_escaping_root_raises_on_windows(tmp_path: Path) -> None:
    """On Windows a backslash component escaping the root is rejected."""
    backend = LocalBackend(settings=LocalStorageSettings(base_dir=str(tmp_path)))
    with pytest.raises(StorageKeyError):
        backend.upload("..\\win", b"x")


@pytest.mark.parametrize("prefix", ["../", "/etc/"])
def test_list_escaping_prefix_raises(tmp_path: Path, prefix: str) -> None:
    """A prefix resolving outside the root raises StorageKeyError (backend-level guard)."""
    backend = LocalBackend(settings=LocalStorageSettings(base_dir=str(tmp_path)))
    with pytest.raises(StorageKeyError):
        backend.list(prefix)


def test_upload_download_roundtrip_bytes(svc: StorageService) -> None:
    """Bytes upload is retrievable via download."""
    result = svc.upload("a/b.txt", b"hello world")
    assert result.storage_key == "a/b.txt"
    assert result.size == 11
    assert svc.download("a/b.txt") == b"hello world"


def test_upload_download_roundtrip_path(svc: StorageService, tmp_path: Path) -> None:
    """Path source upload is retrievable via download."""
    source = tmp_path / "source.bin"
    source.write_bytes(b"\x00\x01\x02")
    svc.upload("blob.bin", source)
    assert svc.download("blob.bin") == b"\x00\x01\x02"


def test_upload_file_like_source(svc: StorageService) -> None:
    """A file-like source uploads correctly."""
    import io

    svc.upload("stream.txt", io.BytesIO(b"from-io"))
    assert svc.download("stream.txt") == b"from-io"


def test_stream_chunks(svc: StorageService) -> None:
    """stream() yields the full content in chunks."""
    svc.upload("big.txt", b"x" * 100)
    assert list(svc.stream("big.txt", chunk_size=32)) == [b"x" * 32, b"x" * 32, b"x" * 32, b"x" * 4]


def test_stream_missing_raises(svc: StorageService) -> None:
    """stream() on a missing object raises StorageObjectNotFoundError."""
    with pytest.raises(StorageObjectNotFoundError):
        list(svc.stream("nope.txt"))


def test_download_missing_raises(svc: StorageService) -> None:
    """download() on a missing object raises StorageObjectNotFoundError."""
    with pytest.raises(StorageObjectNotFoundError):
        svc.download("nope.txt")


def test_head_and_delete(svc: StorageService) -> None:
    """head() reports metadata; delete() returns existence."""
    svc.upload("h.txt", b"data", mime_type="text/plain", provider_metadata={"owner": "alice"})
    obj = svc.head("h.txt")
    assert obj is not None
    assert obj.size == 4
    assert obj.mime_type == "text/plain"
    assert obj.provider_metadata == {"owner": "alice"}
    assert obj.last_modified is not None
    assert obj.last_modified.tzinfo is not None
    assert svc.delete("h.txt") is True
    assert svc.head("h.txt") is None
    assert svc.delete("h.txt") is False


def test_head_infers_mime_type(svc: StorageService) -> None:
    """Without an explicit mime_type, mimetypes guesses from the key."""
    svc.upload("note.md", b"# title")
    obj = svc.head("note.md")
    assert obj is not None
    assert obj.mime_type == "text/markdown"


def test_move_and_copy(svc: StorageService) -> None:
    """move() relocates (source gone); copy() duplicates (source kept)."""
    svc.upload("m.txt", b"payload")
    svc.move("m.txt", "n.txt")
    assert svc.head("m.txt") is None
    assert svc.download("n.txt") == b"payload"
    svc.copy("n.txt", "o.txt")
    assert svc.download("n.txt") == b"payload"
    assert svc.download("o.txt") == b"payload"


def test_move_missing_raises(svc: StorageService) -> None:
    """move() on a missing source raises StorageObjectNotFoundError."""
    with pytest.raises(StorageObjectNotFoundError):
        svc.move("missing.txt", "dest.txt")


def test_list_with_prefix_and_pagination(svc: StorageService) -> None:
    """list() returns immediate children with directory entries and cursor pagination."""
    for key in ("a/1.txt", "a/2.txt", "b/3.txt"):
        svc.upload(key, b"x")

    top = svc.list()
    assert top.items == [
        StorageListEntry(storage_key="a/", is_dir=True),
        StorageListEntry(storage_key="b/", is_dir=True),
    ]
    assert top.truncated is False
    assert top.continuation_token is None

    inside = svc.list("a")
    assert [entry.storage_key for entry in inside.items] == ["a/1.txt", "a/2.txt"]
    assert [entry.size for entry in inside.items] == [1, 1]
    assert all(entry.last_modified is not None for entry in inside.items)
    assert all(entry.is_dir is False for entry in inside.items)

    first = svc.list(limit=1)
    assert first.items == [StorageListEntry(storage_key="a/", is_dir=True)]
    assert first.truncated is True
    assert first.continuation_token == "a/"

    second = svc.list(limit=1, continuation_token=first.continuation_token)
    assert second.items == [StorageListEntry(storage_key="b/", is_dir=True)]
    assert second.truncated is False
    assert second.continuation_token is None


def test_list_excludes_metadata_sidecars(svc: StorageService) -> None:
    """Metadata sidecar files never appear in list() results."""
    svc.upload("m1.txt", b"x", provider_metadata={"k": "v"})
    keys = [item.storage_key for item in svc.list().items]
    assert keys == ["m1.txt"]


def test_provider_metadata_follows_move_and_copy(svc: StorageService) -> None:
    """Metadata sidecars are carried by move() and copy()."""
    svc.upload("src.txt", b"x", provider_metadata={"owner": "alice"})
    svc.move("src.txt", "mid.txt")
    assert svc.head("mid.txt").provider_metadata == {"owner": "alice"}  # type: ignore[union-attr]
    svc.copy("mid.txt", "dst.txt")
    assert svc.head("dst.txt").provider_metadata == {"owner": "alice"}  # type: ignore[union-attr]


def test_move_and_copy_clear_stale_destination_metadata(svc: StorageService) -> None:
    """move()/copy() drop a stale destination sidecar when the source has none."""
    svc.upload("dest.txt", b"stale", provider_metadata={"owner": "alice"})
    svc.upload("plain.txt", b"fresh")
    svc.copy("plain.txt", "dest.txt")
    assert svc.head("dest.txt").provider_metadata == {}  # type: ignore[union-attr]
    svc.upload("dest2.txt", b"stale", provider_metadata={"owner": "alice"})
    svc.move("plain.txt", "dest2.txt")
    assert svc.head("dest2.txt").provider_metadata == {}  # type: ignore[union-attr]


def test_delete_removes_sidecar(svc: StorageService, tmp_path: Path) -> None:
    """delete() also removes the metadata sidecar."""
    svc.upload("d.txt", b"x", provider_metadata={"k": "v"})
    assert (tmp_path / "d.txt.bmeta.json").exists()
    svc.delete("d.txt")
    assert not (tmp_path / "d.txt.bmeta.json").exists()


@pytest.mark.parametrize("op", ["get_signed_url", "get_access_url", "get_preview_url"])
def test_url_operations_unsupported(svc: StorageService, op: str) -> None:
    """The local backend rejects all URL operations."""
    svc.upload("u.txt", b"x")
    with pytest.raises(StorageUrlUnsupportedError):
        getattr(svc, op)("u.txt")


def test_exists(svc: StorageService) -> None:
    """exists() reflects file presence on disk."""
    svc.upload("e.txt", b"x")
    assert svc.exists("e.txt") is True
    assert svc.exists("missing.txt") is False


def test_upload_accepts_acl(svc: StorageService) -> None:
    """acl is accepted and ignored by the local backend."""
    svc.upload("a.txt", b"x", acl="public-read")
    assert svc.download("a.txt") == b"x"
