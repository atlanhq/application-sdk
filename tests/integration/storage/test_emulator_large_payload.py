"""Hermetic integration test: large-payload (>=100 MiB) SDK storage paths (S3 + Azure).

Moved from atlan-openapi-app — these exercise pure SDK code, nothing
OpenAPI-specific, and belong with the rest of the SDK's storage coverage:

* **CloudStore single PUT/GET at >=100 MiB** — ``CloudStore.upload(local_path)`` /
  ``download(key)`` with end-to-end SHA-256, so a single corrupted/truncated
  byte at real-world payload size fails the test.
* **ops chunking path at >=100 MiB** — ``storage.ops.upload_file`` /
  ``download_file``, the only path that actually drives obstore's
  multipart-upload writer + streaming range-GET download, SHA-256 verified.
* **Timeout enforcement** — a 1 ms request timeout + retries disabled (plumbed
  through the SDK's ``ATLAN_OBSTORE_*`` config, i.e. the real binding path) must
  surface a ``StorageError``, proving the timeout config reaches the HTTP layer.

Marked ``storage_emulator`` (deselected by default; run in CI with MinIO +
Azurite sidecars). Reuses the same buckets/containers as the per-cloud binding
tests.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path

import pytest

from application_sdk.storage import ops
from application_sdk.storage.binding import create_store_from_binding
from application_sdk.storage.cloud import CloudStore
from application_sdk.storage.errors import StorageError
from tests.integration.storage.conftest import write_dapr_component

pytestmark = pytest.mark.storage_emulator

# Just over 100 MiB so the multipart writer + range-GET paths actually engage.
_LARGE_BYTES = int(os.environ.get("SDK_LARGE_PAYLOAD_MIB", "101")) * 1024 * 1024

# --- S3 / MinIO ---
_S3_ENDPOINT = os.environ.get("AWS_ENDPOINT_URL", "http://localhost:9000")
_S3_USER = os.environ.get("MINIO_ROOT_USER", "minioadmin")
_S3_PASS = os.environ.get("MINIO_ROOT_PASSWORD", "minioadmin")
_S3_BUCKET = os.environ.get("S3_EMULATOR_BUCKET", "sdk-emulator-test")

# --- Azure / Azurite (well-known public dev account/key, emulator-only) ---
_AZ_ACCOUNT = "devstoreaccount1"
_AZ_KEY = (
    "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/"
    "K1SZFPTOtr/KBHBeksoGMGw=="
)
_AZ_ENDPOINT = os.environ.get(
    "AZURE_STORAGE_ENDPOINT", "http://localhost:10000"
).rstrip("/")
_AZ_BLOB_ENDPOINT = f"{_AZ_ENDPOINT}/{_AZ_ACCOUNT}"
_AZ_CONTAINER = os.environ.get("AZURE_EMULATOR_CONTAINER", "sdk-emulator-test")


def _sha256_of_path(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        while chunk := f.read(8 * 1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


@pytest.fixture(scope="module")
def large_payload_file(tmp_path_factory) -> tuple[Path, str, int]:
    """A >=100 MiB random file, generated once per module. Returns (path, sha256, size)."""
    path = tmp_path_factory.mktemp("sdk-large-payload") / "payload.bin"
    block = secrets.token_bytes(8 * 1024 * 1024)
    h = hashlib.sha256()
    written = 0
    with path.open("wb") as f:
        while written < _LARGE_BYTES:
            chunk = block[: min(len(block), _LARGE_BYTES - written)]
            f.write(chunk)
            h.update(chunk)
            written += len(chunk)
    return path, h.hexdigest(), written


def _reachable(url: str) -> bool:
    import httpx

    try:
        with httpx.Client(timeout=3.0) as client:
            return client.get(url).status_code < 500
    except Exception:  # pragma: no cover — env guard
        return False


def _s3_cloud_store(components_dir) -> CloudStore:
    write_dapr_component(
        components_dir,
        name="s3-large",
        binding_type="bindings.aws.s3",
        metadata={
            "bucket": _S3_BUCKET,
            "region": "us-east-1",
            "endpoint": _S3_ENDPOINT,
            "forcePathStyle": "true",
            "accessKey": _S3_USER,
            "secretKey": _S3_PASS,
        },
    )
    return CloudStore(
        create_store_from_binding("s3-large", components_dir=components_dir),
        provider="s3",
    )


def _azure_cloud_store(components_dir) -> CloudStore:
    write_dapr_component(
        components_dir,
        name="azure-large",
        binding_type="bindings.azure.blobstorage",
        metadata={
            "accountName": _AZ_ACCOUNT,
            "accountKey": _AZ_KEY,
            "containerName": _AZ_CONTAINER,
            "endpoint": _AZ_BLOB_ENDPOINT,
        },
    )
    return CloudStore(
        create_store_from_binding("azure-large", components_dir=components_dir),
        provider="azure",
    )


@pytest.fixture(params=["s3", "azure"])
def cloud_store(request, tmp_path) -> CloudStore:
    """Parametrized CloudStore over MinIO (S3) + Azurite (Azure)."""
    if request.param == "s3":
        if not _reachable(f"{_S3_ENDPOINT}/minio/health/live"):
            pytest.skip("MinIO not reachable")
        return _s3_cloud_store(tmp_path / "components")
    if not _reachable(_AZ_BLOB_ENDPOINT):
        pytest.skip("Azurite not reachable")
    return _azure_cloud_store(tmp_path / "components")


async def test_large_payload_round_trip(cloud_store, large_payload_file, tmp_path):
    """>=100 MiB through CloudStore.upload/download (single PUT/GET), SHA-256 checked."""
    src_path, src_sha, src_size = large_payload_file
    key = "large/payload.bin"
    try:
        assert await cloud_store.upload(local_path=src_path, key=key) == src_size
        paths = await cloud_store.download(key=key, output_dir=str(tmp_path / "dl"))
        assert len(paths) == 1
        assert paths[0].stat().st_size == src_size
        assert _sha256_of_path(paths[0]) == src_sha
    finally:
        import contextlib

        with contextlib.suppress(Exception):
            await ops.delete(key, store=cloud_store.store)


async def test_large_payload_via_ops_chunking(
    cloud_store, large_payload_file, tmp_path
):
    """>=100 MiB through ops.upload_file/download_file (multipart writer + range-GET)."""
    src_path, src_sha, src_size = large_payload_file
    key = "large-chunked/payload.bin"
    try:
        digest = await ops.upload_file(
            key, src_path, store=cloud_store.store, normalize=False
        )
        assert digest == src_sha
        dl = tmp_path / "chunked.bin"
        dl_digest = await ops.download_file(
            key, dl, store=cloud_store.store, compute_hash=True, normalize=False
        )
        assert dl.stat().st_size == src_size
        assert dl_digest == src_sha
    finally:
        import contextlib

        with contextlib.suppress(Exception):
            await ops.delete(key, store=cloud_store.store)


async def test_upload_respects_short_request_timeout(
    large_payload_file, tmp_path, monkeypatch
):
    """A 1ms request timeout + retries disabled must surface a StorageError.

    The timeout/retry are plumbed through the SDK's own ``ATLAN_OBSTORE_*``
    config (the real ``create_store_from_binding`` path), so this proves the
    timeout reaches obstore's HTTP client. S3/MinIO is representative — the
    propagation is cloud-agnostic.
    """
    if not _reachable(f"{_S3_ENDPOINT}/minio/health/live"):
        pytest.skip("MinIO not reachable")
    monkeypatch.setenv("ATLAN_OBSTORE_TIMEOUT", "1ms")
    monkeypatch.setenv("ATLAN_OBSTORE_RETRY_MAX_RETRIES", "0")
    cs = _s3_cloud_store(tmp_path / "components")  # built AFTER the env overrides

    src_path, _, _ = large_payload_file
    with pytest.raises(StorageError) as exc_info:
        await cs.upload(local_path=src_path, key="timeout-test/upload.bin")
    rendered = str(exc_info.value).lower()
    assert any(
        m in rendered for m in ("timeout", "timed out", "deadline", "elapsed")
    ), f"expected a timeout-related error, got: {exc_info.value!r}"
