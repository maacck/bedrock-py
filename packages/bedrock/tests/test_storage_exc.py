"""Tests for the storage exception hierarchy."""

from bedrock.contrib.storage.exc import (
    StorageBackendNotConfiguredError,
    StorageConnectionError,
    StorageDownloadError,
    StorageError,
    StorageKeyError,
    StorageObjectNotFoundError,
    StoragePermissionError,
    StorageUploadError,
    StorageUrlUnsupportedError,
)
from bedrock.exc import BedrockExc


def test_all_storage_exceptions_inherit_storage_error() -> None:
    """Every storage exception must derive from StorageError."""
    for exc_cls in (
        StorageBackendNotConfiguredError,
        StorageConnectionError,
        StorageDownloadError,
        StorageKeyError,
        StorageObjectNotFoundError,
        StoragePermissionError,
        StorageUploadError,
        StorageUrlUnsupportedError,
    ):
        assert issubclass(exc_cls, StorageError)
        assert issubclass(exc_cls, BedrockExc)


def test_storage_error_sets_default_detail() -> None:
    """A bare StorageError carries its class-level detail message."""
    exc = StorageError()
    assert exc.detail
    assert str(exc) == exc.detail


def test_storage_error_msg_overrides_detail() -> None:
    """Passing msg to the constructor overrides the default detail."""
    exc = StorageObjectNotFoundError(msg="Object 'a/b.txt' does not exist.")
    assert exc.detail == "Object 'a/b.txt' does not exist."
    assert str(exc) == exc.detail
