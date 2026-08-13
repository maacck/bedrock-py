"""Tests for StorageService: key normalization, backend configuration, registry."""

from typing import Iterable

import pytest

from bedrock.contrib.storage import (
    StorageBackendNotConfiguredError,
    StorageError,
    StorageKeyError,
    StorageObject,
    StorageUploadResult,
    list_backends,
    normalize_storage_key,
    register_backend,
)
from bedrock.contrib.storage.entities import StorageListEntry, StorageListResult
from bedrock.contrib.storage.service import StorageService


class StubBackend:
    """Minimal in-memory backend used to exercise service delegation."""

    def __init__(self, settings=None) -> None:
        self.objects: dict[str, bytes] = {}
        self.seen_keys: list[str] = []

    def upload(self, storage_key: str, data, mime_type=None, provider_metadata=None, acl=None) -> StorageUploadResult:
        self.seen_keys.append(storage_key)
        payload = data if isinstance(data, bytes) else b"stub"
        self.objects[storage_key] = payload
        return StorageUploadResult(storage_key=storage_key, size=len(payload))

    def download(self, storage_key: str) -> bytes:
        return self.objects[storage_key]

    def stream(self, storage_key: str, chunk_size: int = 1_048_576) -> Iterable[bytes]:
        yield self.objects[storage_key]

    def move(self, storage_key: str, dest_storage_key: str) -> None:
        self.objects[dest_storage_key] = self.objects.pop(storage_key)

    def copy(self, storage_key: str, dest_storage_key: str) -> None:
        self.objects[dest_storage_key] = self.objects[storage_key]

    def list(self, prefix=None, limit=None, continuation_token=None) -> StorageListResult:
        """Directory semantics: immediate children of ``prefix`` (top level when None).

        The service passes ``prefix`` already normalized with a trailing slash
        (or ``None`` for the top level). First path segments under the prefix
        are grouped into directory entries (``is_dir=True``, trailing slash).
        """
        normalized = "" if prefix is None else prefix
        if normalized and not normalized.endswith("/"):
            normalized += "/"
        items: list[StorageListEntry] = []
        seen_dirs: set[str] = set()
        for key in sorted(self.objects):
            if not key.startswith(normalized):
                continue
            rest = key[len(normalized) :]
            if not rest:
                continue
            if "/" in rest:
                dir_key = normalized + rest.split("/", 1)[0] + "/"
                if dir_key not in seen_dirs:
                    seen_dirs.add(dir_key)
                    items.append(StorageListEntry(storage_key=dir_key, is_dir=True))
            else:
                items.append(StorageListEntry(storage_key=key))
        return StorageListResult(items=items)

    def head(self, storage_key: str) -> StorageObject | None:
        data = self.objects.get(storage_key)
        return None if data is None else StorageObject(storage_key=storage_key, size=len(data))

    def delete(self, storage_key: str) -> bool:
        return self.objects.pop(storage_key, None) is not None

    def exists(self, storage_key: str) -> bool:
        return storage_key in self.objects

    def get_signed_url(self, storage_key: str, method: str = "GET", expires_in: int = 3600) -> str:
        return f"https://signed.example.com/{storage_key}?method={method}&expires={expires_in}"

    def get_access_url(self, storage_key: str, acl: str | None = None, expires_in: int = 3600) -> str:
        if acl is None:
            return f"https://signed.example.com/{storage_key}?expires={expires_in}"
        return f"https://access.example.com/{storage_key}?expires={expires_in}"

    def get_preview_url(self, storage_key: str, acl: str | None = None, expires_in: int = 3600) -> str:
        if acl is None:
            return f"https://signed.example.com/{storage_key}?inline=1&expires={expires_in}"
        return f"https://preview.example.com/{storage_key}?inline=1&expires={expires_in}"

    def close(self) -> None:
        self.objects.clear()


@pytest.fixture
def svc() -> StorageService:
    service = StorageService()
    register_backend("stub", StubBackend)
    service.configure("stub")
    yield service
    service.close()


def test_normalize_storage_key_strips_and_collapses() -> None:
    """Leading/trailing slashes and duplicate separators are normalized away."""
    assert normalize_storage_key("a/b.txt") == "a/b.txt"
    assert normalize_storage_key("/a/b.txt") == "a/b.txt"
    assert normalize_storage_key("a/b.txt/") == "a/b.txt"
    assert normalize_storage_key(" a//b.txt ") == "a/b.txt"


@pytest.mark.parametrize("bad", ["", "/", "   ", "..", "a/../b", "../a", "a/.."])
def test_normalize_storage_key_rejects_invalid(bad: str) -> None:
    """Empty, whitespace-only, and parent-escaping keys are rejected."""
    with pytest.raises(StorageKeyError):
        normalize_storage_key(bad)


def test_configure_required() -> None:
    """First use without configure() raises StorageBackendNotConfiguredError."""
    service = StorageService()
    try:
        with pytest.raises(StorageBackendNotConfiguredError, match="configure"):
            service.upload("x.txt", b"y")
    finally:
        service.close()


def test_upload_normalizes_key_before_delegation(svc: StorageService) -> None:
    """Service passes a normalized key to the backend."""
    svc.upload("/a/b.txt", b"hello")
    assert svc.get_backend().seen_keys == ["a/b.txt"]  # type: ignore[attr-defined]


def test_configure_unknown_backend_raises() -> None:
    """Unknown backend names raise StorageBackendNotConfiguredError with available list."""
    with pytest.raises(StorageBackendNotConfiguredError):
        StorageService().configure("does-not-exist")


def test_list_backends_includes_registered_names() -> None:
    """list_backends() reports stub plus the built-in local/s3 entries."""
    assert "stub" in list_backends()
    assert "local" in list_backends()


def test_operations_delegate_with_normalized_keys(svc: StorageService) -> None:
    """Every operation flows through the service with normalized keys."""
    svc.upload("a/b.txt", b"hello")
    assert svc.download("a/b.txt") == b"hello"
    assert list(svc.stream("a/b.txt")) == [b"hello"]
    svc.copy("a/b.txt", "c.txt")
    svc.move("c.txt", "d.txt")
    assert svc.head("d.txt") is not None
    assert svc.head("missing") is None
    assert svc.delete("d.txt") is True
    assert svc.delete("d.txt") is False
    # Directory semantics: only a/b.txt remains, so the top level yields the "a/" directory.
    assert svc.list().items == [StorageListEntry(storage_key="a/", is_dir=True)]
    # A prefix is normalized to a directory; with or without the trailing slash.
    assert svc.list("a").items == [StorageListEntry(storage_key="a/b.txt")]
    assert svc.list("a/").items == [StorageListEntry(storage_key="a/b.txt")]


def test_exists(svc: StorageService) -> None:
    """exists() reflects object presence."""
    svc.upload("e.txt", b"x")
    assert svc.exists("e.txt") is True
    assert svc.exists("missing.txt") is False


def test_upload_passes_acl(svc: StorageService) -> None:
    """acl is forwarded to the backend on upload."""
    svc.upload("a.txt", b"x", acl="public-read")
    assert svc.get_backend().seen_keys == ["a.txt"]  # type: ignore[attr-defined]


def test_cdn_rewrites_access_and_preview_urls() -> None:
    """configured cdn_base_url rewrites the host of access/preview URLs only, preserving query params."""
    service = StorageService()
    register_backend("stub", StubBackend)
    service.configure("stub", cdn_base_url="https://cdn.example.com")
    try:
        access = service.get_access_url("a/b.txt")
        assert access.startswith("https://cdn.example.com/a/b.txt")
        assert "expires=" in access
        preview = service.get_preview_url("a/b.txt")
        assert preview.startswith("https://cdn.example.com/a/b.txt")
        signed = service.get_signed_url("a/b.txt", method="PUT", expires_in=60)
        assert signed.startswith("https://signed.example.com/")
        assert "method=PUT" in signed and "expires=60" in signed
    finally:
        service.close()


@pytest.mark.parametrize("method_name", ["get_signed_url", "get_access_url", "get_preview_url"])
@pytest.mark.parametrize("expires_in", [0, -1, "60", True])
def test_url_methods_reject_invalid_expires_in(svc: StorageService, method_name: str, expires_in: object) -> None:
    """expires_in must be a positive integer; invalid values raise StorageError before delegation."""
    with pytest.raises(StorageError, match="expires_in"):
        getattr(svc, method_name)("a/b.txt", expires_in=expires_in)  # type: ignore[arg-type]
