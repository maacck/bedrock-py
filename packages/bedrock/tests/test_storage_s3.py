"""Tests for the S3-compatible backend against a real local moto server.

moto's ``mock_aws`` only intercepts boto3 calls: presigned URLs point at real
AWS hosts and plain ``requests`` cannot reach the mock. Per amendment A14 the
fixtures below run a real ``moto.server`` on a local port and point
``S3StorageSettings.endpoint_url`` at it, so every generated URL executes over
actual HTTP against the mock.
"""

import io
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable, Iterator

import pytest
import requests

from bedrock.contrib.storage import (
    S3StorageSettings,
    StorageError,
    StorageListEntry,
    StorageObjectNotFoundError,
)
from bedrock.contrib.storage.service import StorageService

_BUCKET = "test-bucket"
_REGION = "us-east-1"


@pytest.fixture(scope="session")
def moto_server() -> Iterator[str]:
    """Run a real moto S3 server so generated URLs can be executed over real HTTP (A14)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    endpoint = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, "-m", "moto.server", "-p", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                requests.get(endpoint, timeout=0.5)
                break
            except requests.RequestException:
                time.sleep(0.05)
        else:
            raise RuntimeError(f"moto server did not become ready on {endpoint}")
        yield endpoint
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def _reset_bucket(endpoint_url: str) -> None:
    """Delete every object and then the bucket so each test starts from an empty bucket."""
    import boto3

    client = boto3.client("s3", region_name=_REGION, endpoint_url=endpoint_url)
    try:
        contents = client.list_objects_v2(Bucket=_BUCKET).get("Contents", [])
        if contents:
            client.delete_objects(Bucket=_BUCKET, Delete={"Objects": [{"Key": o["Key"]} for o in contents]})
        client.delete_bucket(Bucket=_BUCKET)
    except client.exceptions.ClientError:
        pass


def _create_bucket(endpoint_url: str) -> None:
    import boto3

    boto3.client("s3", region_name=_REGION, endpoint_url=endpoint_url).create_bucket(Bucket=_BUCKET)


def _configured_service(
    endpoint_url: str,
    *,
    default_acl: str | None = None,
    cdn_base_url: str | None = None,
) -> StorageService:
    """Build a fresh service + bucket against the moto server."""
    _reset_bucket(endpoint_url)
    _create_bucket(endpoint_url)
    service = StorageService()
    service.configure(
        "s3",
        settings=S3StorageSettings(
            bucket_name=_BUCKET,
            region_name=_REGION,
            endpoint_url=endpoint_url,
            default_acl=default_acl,
        ),
        cdn_base_url=cdn_base_url,
    )
    return service


@pytest.fixture
def svc(moto_server: str) -> Iterable[StorageService]:
    """Configure an S3 backend against a mocked S3 endpoint (real local moto server)."""
    service = _configured_service(moto_server)
    try:
        yield service
    finally:
        service.close()
        _reset_bucket(moto_server)


def test_upload_download_roundtrip(svc: StorageService) -> None:
    """Bytes upload is retrievable via download with accurate size."""
    result = svc.upload("a/b.txt", b"hello s3")
    assert result.storage_key == "a/b.txt"
    assert result.size == 8
    assert svc.download("a/b.txt") == b"hello s3"


def test_upload_path_and_file_like(svc: StorageService, tmp_path: Path) -> None:
    """Path and file-like sources upload correctly."""
    source = tmp_path / "f.bin"
    source.write_bytes(b"\xde\xad\xbe\xef")
    svc.upload("f.bin", source)
    assert svc.download("f.bin") == b"\xde\xad\xbe\xef"

    svc.upload("g.bin", io.BytesIO(b"streamed"))
    assert svc.download("g.bin") == b"streamed"


def test_stream_chunks(svc: StorageService) -> None:
    """stream() yields content chunks in order."""
    svc.upload("big.bin", b"y" * 64)
    assert b"".join(svc.stream("big.bin", chunk_size=16)) == b"y" * 64


def test_download_missing_raises(svc: StorageService) -> None:
    """download() on a missing object raises StorageObjectNotFoundError."""
    with pytest.raises(StorageObjectNotFoundError):
        svc.download("nope.txt")


def test_head_metadata_and_missing(svc: StorageService) -> None:
    """head() returns metadata for existing objects and None for missing."""
    svc.upload("h.txt", b"data", mime_type="text/plain", provider_metadata={"owner": "alice"})
    obj = svc.head("h.txt")
    assert obj is not None
    assert obj.size == 4
    assert obj.mime_type == "text/plain"
    assert obj.provider_metadata == {"owner": "alice"}
    assert obj.last_modified is not None
    assert svc.head("missing.txt") is None


def test_head_infers_mime_type(svc: StorageService) -> None:
    """Without an explicit mime_type, S3 upload guesses from the key (A8)."""
    svc.upload("note.md", b"# title")
    obj = svc.head("note.md")
    assert obj is not None
    assert obj.mime_type == "text/markdown"


def test_move_and_copy(svc: StorageService) -> None:
    """move() relocates; copy() duplicates."""
    svc.upload("m.txt", b"payload")
    svc.move("m.txt", "n.txt")
    assert svc.head("m.txt") is None
    assert svc.download("n.txt") == b"payload"
    svc.copy("n.txt", "o.txt")
    assert svc.download("o.txt") == b"payload"


def test_move_missing_raises(svc: StorageService) -> None:
    """move() on a missing source raises StorageObjectNotFoundError."""
    with pytest.raises(StorageObjectNotFoundError):
        svc.move("missing.txt", "dest.txt")


def test_list_with_prefix_and_pagination(svc: StorageService) -> None:
    """list() returns immediate children with directory entries and cursor pagination (A3)."""
    for key in ("a/1.txt", "a/2.txt", "b/3.txt"):
        svc.upload(key, b"x")

    top = svc.list()
    assert top.items == [
        StorageListEntry(storage_key="a/", is_dir=True),
        StorageListEntry(storage_key="b/", is_dir=True),
    ]
    assert top.truncated is False
    assert top.continuation_token is None

    page_a = svc.list(prefix="a")
    assert [item.storage_key for item in page_a.items] == ["a/1.txt", "a/2.txt"]
    assert [item.size for item in page_a.items] == [1, 1]
    assert all(not item.is_dir for item in page_a.items)

    first = svc.list(limit=1)
    assert [item.storage_key for item in first.items] == ["a/"]
    assert first.truncated is True
    assert first.continuation_token is not None
    second = svc.list(limit=1, continuation_token=first.continuation_token)
    assert [item.storage_key for item in second.items] == ["b/"]
    assert second.truncated is False
    assert second.continuation_token is None


def test_list_interleaves_objects_and_directories(svc: StorageService) -> None:
    """list() keeps S3's lexicographic interleaving of objects and directories (A3).

    Regression: directory prefixes and objects are paginated together in key
    order — a top-level object ``b.txt`` and a directory ``a/`` must return
    ``[a/ (dir), b.txt]``, not ``[b.txt, a/]``.
    """
    svc.upload("a/1.txt", b"x")
    svc.upload("b.txt", b"x")

    top = svc.list()
    assert [item.storage_key for item in top.items] == ["a/", "b.txt"]
    assert top.items[0].is_dir is True
    assert top.items[1].is_dir is False
    assert top.truncated is False


def test_delete_existence_semantics(svc: StorageService) -> None:
    """delete() returns True when the object existed, False otherwise."""
    svc.upload("d.txt", b"x")
    assert svc.delete("d.txt") is True
    assert svc.delete("d.txt") is False


def test_get_signed_url(svc: StorageService, moto_server: str) -> None:
    """get_signed_url() returns a presigned URL for the object."""
    svc.upload("s.txt", b"x")
    url = svc.get_signed_url("s.txt", expires_in=300)
    assert url.startswith(moto_server)
    assert "test-bucket" in url
    assert "X-Amz-Expires=300" in url


def test_get_signed_url_methods(svc: StorageService, moto_server: str) -> None:
    """GET/PUT presign and execute over real HTTP; POST/DELETE are rejected (A14 ruling)."""
    svc.upload("s.txt", b"x")

    get_url = svc.get_signed_url("s.txt", method="GET", expires_in=300)
    assert get_url.startswith(moto_server)
    assert "X-Amz-Expires=300" in get_url
    get_response = requests.get(get_url, timeout=10)
    assert get_response.status_code == 200
    assert get_response.content == b"x"

    put_url = svc.get_signed_url("s.txt", method="PUT", expires_in=300)
    assert put_url.startswith(moto_server)
    assert "X-Amz-Expires=300" in put_url
    put_response = requests.put(put_url, data=b"updated via presigned PUT", timeout=10)
    assert put_response.status_code == 200
    assert svc.download("s.txt") == b"updated via presigned PUT"

    # A14 ruling: POST presigning is unsupported (boto3 has no post_object client
    # method; presigned POST is form-based and a str-returning API cannot express
    # it). Both POST and DELETE are rejected up front with StorageError; real
    # form-protocol POST is covered by test_presigned_post_form_executes.
    with pytest.raises(StorageError, match="Unsupported signed URL method 'POST'"):
        svc.get_signed_url("s.txt", method="POST", expires_in=300)
    with pytest.raises(StorageError, match="Unsupported signed URL method 'DELETE'"):
        svc.get_signed_url("s.txt", method="DELETE")


def test_presigned_post_form_executes(svc: StorageService, moto_server: str) -> None:
    """A real presigned POST upload executes over HTTP via the form protocol (A14 evidence).

    boto3's presigned POST is form-based (``generate_presigned_post`` returns
    ``{url, fields}``), which a single-URL API cannot express; moto serves the
    real form POST (204) and the object becomes downloadable through the service.
    """
    import boto3

    client = boto3.client("s3", region_name=_REGION, endpoint_url=moto_server)
    post = client.generate_presigned_post(_BUCKET, "posted.txt", ExpiresIn=300)
    response = requests.post(
        post["url"],
        data=post["fields"],
        files={"file": ("posted.txt", b"posted via presigned POST", "application/octet-stream")},
        timeout=10,
    )
    assert response.status_code == 204
    assert svc.download("posted.txt") == b"posted via presigned POST"


def test_exists(svc: StorageService) -> None:
    """exists() reflects object presence."""
    svc.upload("e.txt", b"x")
    assert svc.exists("e.txt") is True
    assert svc.exists("missing.txt") is False


def test_upload_acl(svc: StorageService) -> None:
    """A valid canned ACL is applied; content round-trips."""
    svc.upload("a.txt", b"x", acl="public-read")
    assert svc.download("a.txt") == b"x"


def test_upload_invalid_acl_raises(svc: StorageService) -> None:
    """An invalid canned ACL raises StorageError before any API call."""
    with pytest.raises(StorageError):
        svc.upload("bad.txt", b"x", acl="not-an-acl")


def test_get_access_url(svc: StorageService, moto_server: str) -> None:
    """get_access_url() is signed by default and unsigned with a public acl (A6)."""
    svc.upload("a.txt", b"x")
    signed = svc.get_access_url("a.txt", expires_in=300)
    assert signed.startswith(moto_server)
    assert "X-Amz-Expires=300" in signed
    public = svc.get_access_url("a.txt", acl="public-read", expires_in=300)
    assert "X-Amz-Expires" not in public
    assert "test-bucket" in public


def test_get_preview_url(svc: StorageService, moto_server: str) -> None:
    """get_preview_url() requests inline disposition; signed when private, unsigned when public."""
    svc.upload("p.png", b"\x89PNG")
    signed = svc.get_preview_url("p.png", expires_in=300)
    assert signed.startswith(moto_server)
    assert "response-content-disposition=inline" in signed
    public = svc.get_preview_url("p.png", acl="public-read", expires_in=300)
    assert "X-Amz-Expires" not in public
    assert "response-content-disposition=inline" in public


def test_cdn_rewrites_urls(moto_server: str) -> None:
    """CDN rewriting applies to access/preview URLs, never signed URLs (A5)."""
    service = _configured_service(moto_server, cdn_base_url="https://cdn.example.com")
    try:
        service.upload("c.txt", b"x")
        assert service.get_access_url("c.txt").startswith("https://cdn.example.com/")
        assert service.get_preview_url("c.txt").startswith("https://cdn.example.com/")
        signed = service.get_signed_url("c.txt")
        assert not signed.startswith("https://cdn.example.com/")
        assert "test-bucket" in signed
    finally:
        service.close()
        _reset_bucket(moto_server)


def test_default_acl_drives_upload_and_urls(moto_server: str) -> None:
    """Bucket-level default_acl applies when no explicit acl is passed (A6)."""
    service = _configured_service(moto_server, default_acl="public-read")
    try:
        service.upload("d.txt", b"x")  # no explicit acl -> default applies
        assert service.download("d.txt") == b"x"
        assert "X-Amz-Expires" not in service.get_access_url("d.txt")
        explicit = service.get_access_url("d.txt", acl="private")  # explicit wins -> signed
        assert "X-Amz-Expires" in explicit
    finally:
        service.close()
        _reset_bucket(moto_server)


def test_invalid_default_acl_raises_at_configure() -> None:
    """An invalid settings default_acl fails fast at configure() time (A6)."""
    with pytest.raises(StorageError):
        StorageService().configure(
            "s3", settings=S3StorageSettings(bucket_name="test-bucket", default_acl="not-an-acl")
        )
