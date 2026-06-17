"""Hermetic integration test: ``create_store_from_binding`` → S3 against MinIO.

Exercises the SDK's S3 binding wiring + obstore I/O against a local **MinIO**
emulator — no real AWS, and **no production-code changes** (``binding.py``
already maps the ``endpoint`` metadata key and infers ``allow_http`` from an
``http://`` scheme). This is the shared SDK storage logic, tested once, here.

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

import obstore
import pytest

from application_sdk.storage.binding import create_store_from_binding
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


async def test_s3_binding_roundtrip(tmp_path):
    """A static-key S3 component pointed at MinIO round-trips put/list/get/delete."""
    write_dapr_component(
        tmp_path / "components",
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
    store = create_store_from_binding(
        "s3-emulator", components_dir=tmp_path / "components"
    )

    key = "sdk-emulator/roundtrip.txt"
    payload = b"hello-from-sdk-s3-emulator-test"
    await obstore.put_async(store, key, payload)

    listed = [
        obj["path"]
        async for batch in obstore.list(store, prefix="sdk-emulator/")
        for obj in batch
    ]
    assert key in listed

    result = await obstore.get_async(store, key)
    assert bytes(await result.bytes_async()) == payload

    await obstore.delete_async(store, key)
