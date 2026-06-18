"""Hermetic integration test: ``create_store_from_binding`` → S3 (MinIO), via SDK APIs.

Exercises two SDK layers against a local **MinIO** emulator — no real AWS and
**no production-code change** (``binding.py`` already maps ``endpoint`` + infers
``allow_http`` for ``http://``):

1. **Binding wiring** — ``create_store_from_binding`` parses the Dapr component
   into a working store.
2. **The SDK's own storage abstractions** on top of that store — ``CloudStore``
   (``upload_bytes`` / ``list`` / ``get_bytes``) and ``storage.ops``
   (``upload_file`` / ``download_file`` / ``exists`` / ``delete``). The tests
   call the SDK as its users would, not ``obstore`` directly, so an ``obstore``
   API change can't silently pass while the SDK's surface breaks. This also
   gives ``ops.*`` its first cloud-protocol (vs ``LocalStore``) baseline.

Marked ``storage_emulator`` (deselected by default; run in CI with a MinIO
sidecar). Local:

    docker run -d --rm -p 9000:9000 \\
        -e MINIO_ROOT_USER=minioadmin -e MINIO_ROOT_PASSWORD=minioadmin \\
        minio/minio server /data
    AWS_ACCESS_KEY_ID=minioadmin AWS_SECRET_ACCESS_KEY=minioadmin \\
        aws --endpoint-url http://localhost:9000 s3 mb s3://sdk-emulator-test
    uv run pytest tests/integration/storage/test_emulator_s3.py -m storage_emulator -v
"""

from __future__ import annotations

import os

import pytest

from application_sdk.storage import ops
from application_sdk.storage.binding import create_store_from_binding
from application_sdk.storage.cloud import CloudStore
from tests.integration.storage.conftest import write_dapr_component

pytestmark = pytest.mark.storage_emulator

_ENDPOINT = os.environ.get("AWS_ENDPOINT_URL", "http://localhost:9000")
_USER = os.environ.get("MINIO_ROOT_USER", "minioadmin")
_PASS = os.environ.get("MINIO_ROOT_PASSWORD", "minioadmin")
_BUCKET = os.environ.get("S3_EMULATOR_BUCKET", "sdk-emulator-test")


@pytest.fixture(scope="module", autouse=True)
def require_minio() -> None:
    """Skip the module when MinIO isn't reachable or unhealthy."""
    import httpx

    try:
        with httpx.Client(timeout=3.0) as client:
            resp = client.get(f"{_ENDPOINT}/minio/health/live")
    except Exception as exc:  # pragma: no cover — env guard
        pytest.skip(f"MinIO not reachable at {_ENDPOINT}: {exc}")
    if resp.status_code >= 500:  # pragma: no cover — env guard
        pytest.skip(f"MinIO unhealthy at {_ENDPOINT}: HTTP {resp.status_code}")


def _s3_cloud_store(components_dir) -> CloudStore:
    """Build a CloudStore from a static-key S3 Dapr component pointed at MinIO."""
    write_dapr_component(
        components_dir,
        name="s3-emulator",
        binding_type="bindings.aws.s3",
        metadata={
            "bucket": _BUCKET,
            "region": "us-east-1",
            "endpoint": _ENDPOINT,
            "forcePathStyle": "true",
            "accessKey": _USER,
            "secretKey": _PASS,
        },
    )
    store = create_store_from_binding("s3-emulator", components_dir=components_dir)
    return CloudStore(store, provider="s3")


async def test_s3_binding_roundtrip_via_sdk(tmp_path):
    """S3 binding round-trips through CloudStore (bytes) and ops (file) APIs."""
    cs = _s3_cloud_store(tmp_path / "components")

    # CloudStore bytes path: upload_bytes → list → get_bytes.
    key = "sdk-emulator/roundtrip.txt"
    payload = b"hello-from-sdk-s3-emulator-test"
    assert await cs.upload_bytes(key, payload) == len(payload)
    assert key in await cs.list(prefix="sdk-emulator/")
    assert await cs.get_bytes(key) == payload

    # ops file path against a cloud-protocol store (baseline beyond LocalStore):
    # upload_file (multipart writer) → exists → download_file → delete.
    src = tmp_path / "ops-src.txt"
    src.write_bytes(payload)
    ops_key = "sdk-emulator/ops-roundtrip.txt"
    await ops.upload_file(ops_key, src, store=cs.store, normalize=False)
    assert await ops.exists(ops_key, store=cs.store)
    dl = tmp_path / "ops-dl.txt"
    await ops.download_file(ops_key, dl, store=cs.store, normalize=False)
    assert dl.read_bytes() == payload
    assert await ops.delete(ops_key, store=cs.store) is True
    assert not await ops.exists(ops_key, store=cs.store)

    await ops.delete(key, store=cs.store)
