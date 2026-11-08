"""Tests for StorageService: key normalization, backend configuration, registry."""

import base64
import hashlib
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from types import SimpleNamespace

import pytest
from bedrock.common.registry import DuplicateRegistrationError
from bedrock.contrib.storage import (
    StorageBackendNotConfiguredError,
    StorageCloseError,
    StorageError,
    StorageKeyError,
    StorageObject,
    StoragePresignedUrl,
    StorageUploadResult,
    list_backends,
    normalize_storage_key,
    register_backend,
    register_backend_lazy,
)
from bedrock.contrib.storage import service as storage_service_module
from bedrock.contrib.storage.backends.local import LocalStorageSettings
from bedrock.contrib.storage.backends.s3 import S3StorageSettings
from bedrock.contrib.storage.base import StorageBackend
from bedrock.contrib.storage.entities import StorageListEntry, StorageListResult
from bedrock.contrib.storage.service import StorageService
from bedrock.provider import InvalidProviderError, ProviderCatalog, ProviderLoadError
from pydantic_settings import BaseSettings


class StubBackend(StorageBackend):
    """Minimal in-memory backend used to exercise service delegation."""

    def __init__(self, settings=None) -> None:
        self.objects: dict[str, bytes] = {}
        self.seen_keys: list[str] = []
        self._settings = settings or SimpleNamespace()

    @property
    def settings(self) -> BaseSettings:
        """Return the settings used to configure this backend."""
        return self._settings

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
        headers: dict[str, str] = {}
        if mime_type is not None:
            headers["content-type"] = mime_type
        if content_length is not None:
            headers["content-length"] = str(content_length)
        if checksum_sha256 is not None:
            headers["x-checksum"] = checksum_sha256
        headers.update(provider_headers or {})
        return StoragePresignedUrl(
            method=method,
            endpoint=f"https://signed.example.com/{storage_key}",
            query_params={"method": method, "expires": str(expires_in), "sig": "a/b+="},
            headers=headers,
        )

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


class ClosingBackend(StubBackend):
    """Stub backend that records every close() and fails while ``fail`` is set."""

    closed: list["ClosingBackend"] = []
    fail = False

    def close(self) -> None:
        if self.fail:
            raise RuntimeError("close boom")
        type(self).closed.append(self)


@pytest.fixture(autouse=True)
def registry(monkeypatch: pytest.MonkeyPatch) -> ProviderCatalog[StorageBackend]:
    """Give every test a fresh catalog seeded with the built-in lazy backends."""
    fresh = ProviderCatalog(protocol=StorageBackend, specs=storage_service_module._BUILTIN_SPECS)
    monkeypatch.setattr(storage_service_module, "_BACKEND_REGISTRY", fresh)
    ClosingBackend.closed = []
    return fresh


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


def test_configure_wraps_class_load_failure_with_cause(registry: ProviderCatalog[StorageBackend]) -> None:
    """A lazy backend that cannot be imported surfaces as StorageBackendNotConfiguredError chained to the cause."""
    registry.register_lazy("broken", "bedrock_missing_backend_module:Backend")
    with pytest.raises(StorageBackendNotConfiguredError, match="broken") as excinfo:
        StorageService().configure("broken")
    assert isinstance(excinfo.value.__cause__, ProviderLoadError)
    assert isinstance(excinfo.value.__cause__.__cause__, ModuleNotFoundError)


def test_registered_object_that_does_not_implement_protocol_fails_at_configure() -> None:
    """Registration accepts any callable; configure() rejects a result missing protocol members."""

    class Structural:
        def __init__(self, settings: object = None) -> None:
            pass

    register_backend("structural", Structural)  # type: ignore[arg-type]
    with pytest.raises(StorageBackendNotConfiguredError, match="structural") as excinfo:
        StorageService().configure("structural")
    assert isinstance(excinfo.value.__cause__, InvalidProviderError)


def test_register_backend_same_object_is_no_op_but_builtin_name_conflicts() -> None:
    """Re-registering the same class is idempotent; taking a built-in name fails."""
    register_backend("stub", StubBackend)
    register_backend("stub", StubBackend)
    with pytest.raises(DuplicateRegistrationError):
        register_backend("local", StubBackend)
    with pytest.raises(DuplicateRegistrationError):
        register_backend_lazy("s3", "acme_storage:Backend")


def test_register_backend_lazy_imports_only_on_configure(fake_package: Path) -> None:
    """A lazily registered backend module is imported when configure() selects it, not before."""
    (fake_package / "acme_lazy_storage.py").write_text(
        "from bedrock.contrib.storage.backends.local import LocalBackend as Backend\n"
    )
    register_backend_lazy("lazy", "acme_lazy_storage:Backend", install_requirement="acme[x]")
    assert "lazy" in list_backends()
    assert "acme_lazy_storage" not in sys.modules
    StorageService().configure("lazy", settings=LocalStorageSettings(base_dir=str(fake_package / "store")))
    assert "acme_lazy_storage" in sys.modules


def test_close_closes_replaced_and_current_backends_once() -> None:
    """A backend replaced by configure() stays open until close(), which closes each backend once."""
    register_backend("closing", ClosingBackend)
    service = StorageService()
    first = service.configure("closing")
    second = service.configure("closing")
    assert ClosingBackend.closed == []

    service.close()
    service.close()

    assert ClosingBackend.closed == [first, second]
    with pytest.raises(StorageBackendNotConfiguredError):
        service.get_backend()


def test_close_failure_raises_and_retries_the_failed_backend() -> None:
    """A replaced backend whose close() fails is reported and retried by the next close()."""
    register_backend("closing", ClosingBackend)
    service = StorageService()
    first = service.configure("closing")
    second = service.configure("closing")
    first.fail = True  # type: ignore[attr-defined]

    with pytest.raises(StorageCloseError, match="close boom") as excinfo:
        service.close()
    assert isinstance(excinfo.value.__cause__, ExceptionGroup)
    assert ClosingBackend.closed == [second]

    first.fail = False  # type: ignore[attr-defined]
    service.close()
    assert ClosingBackend.closed == [second, first]


def test_failed_configure_keeps_the_previous_backend() -> None:
    """configure() that cannot create a backend leaves the active backend in place."""
    register_backend("stub", StubBackend)
    service = StorageService()
    backend = service.configure("stub")
    with pytest.raises(StorageBackendNotConfiguredError):
        service.configure("missing")
    assert service.get_backend() is backend


def test_importing_storage_loads_no_backend_module() -> None:
    """The package root exports backend classes lazily."""
    code = (
        "import sys, bedrock.contrib.storage as s; "
        "print('bedrock.contrib.storage.backends.s3' in sys.modules, s.S3Backend.__name__)"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert result.stdout.split() == ["False", "S3Backend"]


def test_missing_s3_sdk_names_the_install_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without boto3, configure('s3') names the extra that installs it."""
    monkeypatch.setitem(sys.modules, "boto3", None)
    with pytest.raises(StorageBackendNotConfiguredError, match=r"bedrock-core\[storage-s3\]"):
        StorageService().configure("s3", settings=S3StorageSettings(bucket_name="b"))


def test_list_backends_includes_registered_names() -> None:
    """list_backends() reports a registered backend alongside the built-in local/s3 entries."""
    register_backend("stub", StubBackend)
    assert list_backends() == ["local", "s3", "stub"]


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
    """A settings-level cdn_base_url rewrites the host of access/preview URLs only, preserving query params."""
    service = StorageService()
    register_backend("stub", StubBackend)
    service.configure("stub", settings=SimpleNamespace(cdn_base_url="https://cdn.example.com"))
    try:
        access = service.get_access_url("a/b.txt")
        assert access.startswith("https://cdn.example.com/a/b.txt")
        assert "expires=" in access
        preview = service.get_preview_url("a/b.txt")
        assert preview.startswith("https://cdn.example.com/a/b.txt")
        presigned = service.generate_presigned_url("a/b.txt", method="PUT", expires_in=60)
        assert presigned.endpoint == "https://signed.example.com/a/b.txt"
    finally:
        service.close()


@pytest.mark.parametrize("method_name", ["generate_presigned_url", "get_access_url", "get_preview_url"])
@pytest.mark.parametrize("expires_in", [0, -1, "60", True])
def test_url_methods_reject_invalid_expires_in(svc: StorageService, method_name: str, expires_in: object) -> None:
    """expires_in must be a positive integer; invalid values raise StorageError before delegation."""
    with pytest.raises(StorageError, match="expires_in"):
        getattr(svc, method_name)("a/b.txt", expires_in=expires_in)  # type: ignore[arg-type]


_SHA256_B64 = base64.b64encode(hashlib.sha256(b"body").digest()).decode()


def test_generate_presigned_url_normalizes_inputs(svc: StorageService) -> None:
    """Key and method are normalized; provider header names are lowercased before delegation."""
    presigned = svc.generate_presigned_url(
        "/a//b.txt",
        method="put",
        mime_type="text/plain",
        content_length=0,
        checksum_sha256=_SHA256_B64,
        provider_headers={"X-Amz-Checksum-Type": "FULL_OBJECT"},
    )
    assert presigned.method == "PUT"
    assert presigned.endpoint == "https://signed.example.com/a/b.txt"
    assert presigned.headers == {
        "content-type": "text/plain",
        "content-length": "0",
        "x-checksum": _SHA256_B64,
        "x-amz-checksum-type": "FULL_OBJECT",
    }


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"method": "POST"}, "Unsupported presigned URL method"),
        ({"method": "GET", "mime_type": "text/plain"}, "only be signed for PUT"),
        ({"method": "GET", "checksum_sha256": _SHA256_B64}, "only be signed for PUT"),
        ({"method": "PUT", "content_length": -1}, "content_length"),
        ({"method": "PUT", "content_length": True}, "content_length"),
        ({"method": "PUT", "mime_type": ""}, "mime_type"),
        ({"method": "PUT", "mime_type": "text/plain\r\nx-evil: 1"}, "CR, LF"),
        ({"method": "PUT", "checksum_sha256": hashlib.sha256(b"body").hexdigest()}, "32-byte"),
        ({"method": "PUT", "checksum_sha256": "not base64!"}, "valid base64"),
        ({"method": "PUT", "checksum_sha256": base64.b64encode(b"short").decode()}, "32-byte"),
        ({"method": "PUT", "provider_headers": {"bad name": "x"}}, "Invalid provider header name"),
        ({"method": "PUT", "provider_headers": {"x-a": "1\n2"}}, "CR, LF"),
        ({"method": "PUT", "provider_headers": {"Content-Type": "text/plain"}}, "typed argument"),
        ({"method": "PUT", "provider_headers": {"X-A": "1", "x-a": "2"}}, "Duplicate provider header"),
    ],
)
def test_generate_presigned_url_rejects_invalid_conditions(svc: StorageService, kwargs: dict, match: str) -> None:
    """Invalid methods, conditions, and provider headers raise StorageError before reaching the backend."""
    with pytest.raises(StorageError, match=match):
        svc.generate_presigned_url("a/b.txt", **kwargs)


def test_get_signed_url_is_deprecated_and_returns_full_url(svc: StorageService) -> None:
    """The deprecated helper warns and joins endpoint with the encoded query parameters."""
    with pytest.warns(DeprecationWarning, match="generate_presigned_url"):
        url = svc.get_signed_url("a/b.txt", method="PUT", expires_in=60)
    assert url == "https://signed.example.com/a/b.txt?method=PUT&expires=60&sig=a%2Fb%2B%3D"
