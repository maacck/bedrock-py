"""Tests for storage domain entities."""

from datetime import UTC, datetime

from bedrock.contrib.storage.entities import (
    StorageListEntry,
    StorageListResult,
    StorageObject,
    StorageUploadResult,
)


def test_storage_object_defaults() -> None:
    """StorageObject requires only storage_key and size."""
    obj = StorageObject(storage_key="a/b.txt", size=42)
    assert obj.storage_key == "a/b.txt"
    assert obj.size == 42
    assert obj.etag is None
    assert obj.last_modified is None
    assert obj.mime_type is None
    assert obj.provider_metadata == {}


def test_storage_object_full_fields() -> None:
    """All StorageObject fields round-trip."""
    modified = datetime(2026, 1, 1, tzinfo=UTC)
    obj = StorageObject(
        storage_key="a/b.txt",
        size=42,
        etag="abc123",
        last_modified=modified,
        mime_type="text/plain",
        provider_metadata={"owner": "alice"},
    )
    assert obj.etag == "abc123"
    assert obj.last_modified == modified
    assert obj.mime_type == "text/plain"
    assert obj.provider_metadata == {"owner": "alice"}


def test_storage_upload_result() -> None:
    """StorageUploadResult carries key, size, optional etag."""
    result = StorageUploadResult(storage_key="a/b.txt", size=42, etag="etag-1")
    assert result.storage_key == "a/b.txt"
    assert result.size == 42
    assert result.etag == "etag-1"


def test_storage_list_result_pagination() -> None:
    """StorageListResult carries items and pagination state."""
    entry = StorageListEntry(storage_key="a/b.txt", size=42)
    result = StorageListResult(items=[entry], continuation_token="a/b.txt", truncated=True)
    assert result.items == [entry]
    assert result.continuation_token == "a/b.txt"
    assert result.truncated is True
    assert StorageListResult(items=[]).truncated is False
    assert StorageListResult(items=[]).continuation_token is None


def test_storage_list_entry_is_dir_defaults_to_false() -> None:
    """A StorageListEntry is an object unless is_dir is set explicitly."""
    entry = StorageListEntry(storage_key="a/b.txt", size=42)
    assert entry.is_dir is False


def test_storage_list_entry_directory_round_trip() -> None:
    """A directory entry (trailing-slash key, no size/timestamp) round-trips."""
    entry = StorageListEntry(storage_key="a/", size=None, last_modified=None, is_dir=True)
    assert entry.storage_key == "a/"
    assert entry.size is None
    assert entry.last_modified is None
    assert entry.is_dir is True
