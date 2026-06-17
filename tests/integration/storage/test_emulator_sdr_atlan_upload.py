"""Hermetic integration test: the SDR ``ENABLE_ATLAN_UPLOAD`` handoff.

In a Self-Deployed-Runtime deployment the extract app writes its artifacts to
the **customer-configured** object store (the ``objectstore`` /
``DEPLOYMENT_OBJECT_STORE_NAME`` binding), then hands them off to Atlan's
**internal** object store (``atlan-objectstore`` / ``UPSTREAM_OBJECT_STORE_NAME``)
so the publish app — running in Atlan's infrastructure — can consume them. That
handoff is what ``ENABLE_ATLAN_UPLOAD=true`` gates in production.

The production path is two parts, both exercised here against two MinIO buckets
(customer + atlan), with **no production-code changes**:

1. **Routing** — ``App.upload()`` selects the upstream store when one is
   configured: ``store = self.context.upstream_storage or self.context.storage``
   (``application_sdk/app/base.py``). This test builds a real ``AppContext`` with
   both stores bound to real emulator buckets and asserts the upstream (Atlan)
   store is the one chosen.
2. **Transfer** — ``App.upload()`` delegates to
   ``application_sdk.storage.transfer.upload`` with ``store=<upstream>`` and
   ``_source_store=<customer>``. When the file isn't present on the local pod
   (the cross-pod / KEDA-scaled SDR case) it streams the object directly from
   the customer store to the Atlan store via ``_upload_from_store``. This test
   writes an "extracted asset" into the customer bucket and drives that exact
   delegate, then asserts the bytes land in the Atlan bucket (and the source
   is left intact).

This is the object-store equivalent of proving the layer-1 → layer-2 transfer:
customer-managed storage → internal Atlan storage. Marked ``storage_emulator``
(deselected by default; run in CI with the MinIO sidecar). Local:

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


def test_sdr_upload_routes_to_upstream_atlan_store(tmp_path):
    """``App.upload`` routing: when the upstream (Atlan) store is configured it
    is preferred over the customer/deployment store — the SDR selection rule
    ``context.upstream_storage or context.storage``, exercised with real stores.
    """
    from application_sdk.app.context import AppContext

    components = tmp_path / "components"
    customer_store = _s3_store("objectstore", _CUSTOMER_BUCKET, components)
    atlan_store = _s3_store("atlan-objectstore", _ATLAN_BUCKET, components)

    sdr_ctx = AppContext(
        app_name="sdr-handoff-test",
        app_version="1",
        _storage=customer_store,
        _upstream_storage=atlan_store,
    )
    # This is the exact expression App.upload() evaluates to pick its target.
    assert (sdr_ctx.upstream_storage or sdr_ctx.storage) is atlan_store

    # Non-SDR control: no upstream configured → falls back to the customer store.
    non_sdr_ctx = AppContext(
        app_name="non-sdr-test", app_version="1", _storage=customer_store
    )
    assert non_sdr_ctx.upstream_storage is None
    assert (non_sdr_ctx.upstream_storage or non_sdr_ctx.storage) is customer_store


async def test_sdr_atlan_upload_handoff_transfers_asset(tmp_path):
    """An extracted asset in the customer store is uploaded to the Atlan store.

    Reproduces the SDR cross-pod handoff: the file is absent locally, so
    ``transfer.upload`` streams it from the customer (source) store to the Atlan
    (upstream) store — the same call ``App.upload`` makes under
    ``ENABLE_ATLAN_UPLOAD``.
    """
    from application_sdk.contracts.types import FileReference, StorageTier
    from application_sdk.storage.transfer import upload as transfer_upload

    components = tmp_path / "components"
    customer_store = _s3_store("objectstore", _CUSTOMER_BUCKET, components)
    atlan_store = _s3_store("atlan-objectstore", _ATLAN_BUCKET, components)

    # 1. Extract step output: an asset written to the customer object store.
    source_key = "artifacts/apps/sdr-app/workflows/run-1/extracted/assets.parquet"
    target_key = "artifacts/apps/sdr-app/workflows/run-1/published/assets.parquet"
    payload = b"PAR1-sdr-extracted-asset-bytes-customer-objectstore"
    await obstore.put_async(customer_store, source_key, payload)

    # Sanity: the Atlan store does not have it yet.
    with pytest.raises(Exception):
        await obstore.get_async(atlan_store, target_key)

    # 2. The handoff: stream customer-store → atlan-store via the real delegate.
    #    local_path="" forces the deployment-store fallback (cross-pod SDR case),
    #    mirroring App.upload(store=upstream, _source_store=customer, _source_ref=...).
    result = await transfer_upload(
        "",
        target_key,
        store=atlan_store,
        _source_ref=FileReference(local_path="assets.parquet", storage_path=source_key),
        _source_store=customer_store,
        _tier=StorageTier.RETAINED,
    )

    assert result.synced is True
    assert result.reason == "uploaded"

    # 3. The asset now lives in the Atlan (upstream) store, byte-for-byte.
    landed = await obstore.get_async(atlan_store, target_key)
    assert bytes(await landed.bytes_async()) == payload

    # 4. The source asset is left intact (upload copies, it does not move).
    src = await obstore.get_async(customer_store, source_key)
    assert bytes(await src.bytes_async()) == payload

    # Cleanup so reruns against a persistent emulator stay idempotent.
    await obstore.delete_async(customer_store, source_key)
    await obstore.delete_async(atlan_store, target_key)


async def test_sdr_atlan_upload_handoff_transfers_directory(tmp_path):
    """The realistic SDR shape: a *directory* of extracted files is handed off.

    Extract output is normally a tree of parquet files, not a single object, so
    ``App.upload`` passes the run prefix and ``transfer.upload`` takes its
    directory-fallback branch — listing every key under the source prefix in the
    customer store and streaming each to the Atlan store. This exercises that
    multi-file branch (distinct from the single-file path above).
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
