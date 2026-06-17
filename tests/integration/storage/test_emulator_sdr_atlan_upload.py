"""Hermetic integration test: the SDR ``ENABLE_ATLAN_UPLOAD`` *directory* handoff.

In a Self-Deployed-Runtime deployment the extract app writes its artifacts to
the **customer-configured** object store (``objectstore``), then ``App.upload()``
hands them off to Atlan's **internal** object store (``atlan-objectstore``) for
the publish app to consume.

The routing (``App.upload`` prefers ``upstream_storage``) and the *single-file*
cross-pod fallback are already pinned against the real ``App.upload`` in
``tests/integration/test_app_upload_routing.py`` (cases a/b/d/e). The one branch
that exercises NOTHING there is the **directory** (multi-file) cross-store
fallback — extract output is normally a tree of parquet files, which goes
through ``transfer.upload``'s directory branch (``_upload_from_store`` per key),
distinct from the single-file path. This test closes that gap against two MinIO
buckets, with **no production-code change**.

Marked ``storage_emulator`` (deselected by default; run in CI with the MinIO
sidecar). Local:

    docker run -d --rm -p 9000:9000 \\
        -e MINIO_ROOT_USER=minioadmin -e MINIO_ROOT_PASSWORD=minioadmin \\
        minio/minio server /data
    AWS_ACCESS_KEY_ID=minioadmin AWS_SECRET_ACCESS_KEY=minioadmin \\
        aws --endpoint-url http://localhost:9000 s3 mb s3://sdk-customer-objectstore
    AWS_ACCESS_KEY_ID=minioadmin AWS_SECRET_ACCESS_KEY=minioadmin \\
        aws --endpoint-url http://localhost:9000 s3 mb s3://sdk-atlan-objectstore
    uv run pytest tests/integration/storage/test_emulator_sdr_atlan_upload.py \\
        -m storage_emulator -v
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
# Two distinct buckets standing in for SDR's two object-storage layers.
_CUSTOMER_BUCKET = os.environ.get(
    "CUSTOMER_OBJECT_STORE_BUCKET", "sdk-customer-objectstore"
)
_ATLAN_BUCKET = os.environ.get("ATLAN_OBJECT_STORE_BUCKET", "sdk-atlan-objectstore")


@pytest.fixture(scope="module", autouse=True)
def require_minio() -> None:
    """Skip the module when MinIO isn't reachable."""
    import httpx

    try:
        with httpx.Client(timeout=3.0) as client:
            client.get(f"{_ENDPOINT}/minio/health/live")
    except Exception as exc:  # pragma: no cover — env guard
        pytest.skip(f"MinIO not reachable at {_ENDPOINT}: {exc}")


def _s3_store(component_name: str, bucket: str, components_dir):
    """Build a real obstore S3 store from a Dapr component pointed at MinIO."""
    write_dapr_component(
        components_dir,
        name=component_name,
        binding_type="bindings.aws.s3",
        metadata={
            "bucket": bucket,
            "region": "us-east-1",
            "endpoint": _ENDPOINT,
            "forcePathStyle": "true",
            "accessKey": _USER,
            "secretKey": _PASS,
        },
    )
    return create_store_from_binding(component_name, components_dir=components_dir)


async def test_sdr_atlan_upload_handoff_transfers_directory(tmp_path):
    """A *directory* of extracted files is handed off customer-store → atlan-store.

    Extract output is a tree of parquet files, so ``transfer.upload`` takes its
    directory-fallback branch — listing every key under the source prefix in the
    customer store and streaming each to the Atlan store. (Single-file + routing
    are covered in ``tests/integration/test_app_upload_routing.py``; this pins the
    multi-file branch, which those don't reach.)
    """
    from application_sdk.contracts.types import FileReference, StorageTier
    from application_sdk.storage.transfer import upload as transfer_upload

    components = tmp_path / "components"
    customer_store = _s3_store("objectstore", _CUSTOMER_BUCKET, components)
    atlan_store = _s3_store("atlan-objectstore", _ATLAN_BUCKET, components)

    # 1. Extract output: several files under the run's extracted/ prefix.
    src_prefix = "artifacts/apps/sdr-app/workflows/run-2/extracted"
    dst_prefix = "artifacts/apps/sdr-app/workflows/run-2/published"
    files = {
        "db1/schema1/tables.parquet": b"PAR1-tables-bytes",
        "db1/schema1/columns.parquet": b"PAR1-columns-bytes",
        "db2/views.parquet": b"PAR1-views-bytes",
    }
    for rel, data in files.items():
        await obstore.put_async(customer_store, f"{src_prefix}/{rel}", data)

    # 2. Directory handoff: customer-store prefix → atlan-store prefix.
    result = await transfer_upload(
        "",
        dst_prefix,
        store=atlan_store,
        _source_ref=FileReference(local_path="extracted", storage_path=src_prefix),
        _source_store=customer_store,
        _tier=StorageTier.RETAINED,
    )

    assert result.synced is True
    assert result.ref.file_count == len(files)

    # 3. Every file landed in the Atlan store, byte-for-byte, under dst_prefix.
    for rel, data in files.items():
        landed = await obstore.get_async(atlan_store, f"{dst_prefix}/{rel}")
        assert bytes(await landed.bytes_async()) == data

    # Cleanup so reruns against a persistent emulator stay idempotent.
    for rel in files:
        await obstore.delete_async(customer_store, f"{src_prefix}/{rel}")
        await obstore.delete_async(atlan_store, f"{dst_prefix}/{rel}")
