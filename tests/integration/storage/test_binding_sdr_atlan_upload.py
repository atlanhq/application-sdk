"""Real-cloud SDR object-store handoff — assets land with the right count + location.

The SDR / customer-infra upload path streams a *directory* of extracted files
from the customer object-store to the Atlan object-store via
``transfer.upload``'s directory branch (``_upload_from_store`` per key). The
hermetic version of this (``test_emulator_sdr_atlan_upload.py``) runs it
against two MinIO buckets. This module runs the **same** handoff against the
**real** provisioned cloud buckets (S3 / Azure Blob / GCS) using KEYLESS auth
(GitHub OIDC in CI — no static secrets), so a real-cloud-only regression in the
upload path (wrong count reaching the bucket, or files landing at the wrong
key) fails here rather than in a customer's self-deployed runtime.

Why one bucket + two prefixes (not two buckets)?
    The provisioned keyless OIDC role is scoped to a single integ bucket per
    cloud (``S3_BUCKET`` / ``AZURE_STORAGE_CONTAINER`` / ``GCS_BUCKET``). SDR's
    two object-storage *layers* (customer store → Atlan store) are represented
    by two prefixes inside that one bucket. The ``transfer.upload`` code path is
    identical either way — obstore ``get`` from the source store, ``put`` to the
    target store — so the real-cloud get/put round-trip, count, and location are
    all exercised; only cross-bucket auth (which the per-binding auth tests
    already cover) is not.

Run locally (S3 example)::

    export S3_BUCKET=<bucket> AWS_DEFAULT_REGION=<region>   # + ambient creds
    uv run pytest tests/integration/storage/test_binding_sdr_atlan_upload.py \
        -m s3_integration -v

CI provides all three via the keyless OIDC/WIF steps; all markers are
deselected by default (pyproject addopts), and the autouse ``require_*``
guards in conftest skip when creds are absent.
"""

from __future__ import annotations

import os
import uuid

import obstore
import pytest

from application_sdk.contracts.types import FileReference, StorageTier
from application_sdk.storage import ops
from application_sdk.storage.binding import create_store_from_binding
from application_sdk.storage.cloud import CloudStore
from application_sdk.storage.transfer import upload as transfer_upload
from tests.integration.storage.conftest import (
    AZURE_ACCOUNT,
    AZURE_CLIENT_ID,
    AZURE_CONTAINER,
    AZURE_FEDERATED_TOKEN_FILE,
    AZURE_TENANT_ID,
    GCS_BUCKET,
    GCS_PROJECT_ID,
    S3_BUCKET,
    S3_REGION,
    write_dapr_component,
)

# Extract output is a tree of parquet-ish files under the run's extracted/
# prefix — several keys so transfer.upload takes its directory-fallback branch.
_FILES = {
    "db1/schema1/tables.parquet": b"PAR1-tables-bytes",
    "db1/schema1/columns.parquet": b"PAR1-columns-bytes",
    "db2/views.parquet": b"PAR1-views-bytes",
}


def _run_tag(provider: str) -> str:
    """Unique per provider + CI run so concurrent runs don't collide in the
    shared integ bucket (and a crashed run's leftovers never poison another)."""
    run = os.environ.get("GITHUB_RUN_ID", "local")
    return f"integ-sdr-handoff/{provider}-{run}-{uuid.uuid4().hex[:8]}"


async def _list_keys(store, prefix: str) -> set[str]:
    """Every object key under *prefix* in the real bucket (dict-shaped
    ObjectMeta items, ``item['path']`` — same access ops.list uses)."""
    keys: set[str] = set()
    async for batch in obstore.list(store, prefix=prefix):
        for item in batch:
            keys.add(str(item["path"]))
    return keys


async def _assert_sdr_handoff_lands_assets(store, provider: str) -> None:
    """Seed a customer-store prefix, hand it off to an Atlan-store prefix, and
    assert the extracted files land in the real bucket with the correct COUNT
    and at the correct LOCATION (byte-for-byte, no strays)."""
    tag = _run_tag(provider)
    src_prefix = f"{tag}/extracted"  # customer object-store layer
    dst_prefix = f"{tag}/published"  # Atlan object-store layer
    cs = CloudStore(store, provider=provider)

    try:
        # 1. Extract output: several files under the run's extracted/ prefix.
        for rel, data in _FILES.items():
            await cs.upload_bytes(f"{src_prefix}/{rel}", data)

        # 2. Directory handoff: customer-store prefix → Atlan-store prefix.
        #    Same raw store stands in for both SDR layers (single integ bucket).
        result = await transfer_upload(
            "",
            dst_prefix,
            store=store,
            _source_ref=FileReference(
                local_path="extracted", storage_path=src_prefix
            ),
            _source_store=store,
            _tier=StorageTier.RETAINED,
        )

        # 3a. COUNT — every extracted file was transferred, no more, no fewer.
        assert result.synced is True
        assert result.ref.file_count == len(_FILES), (
            f"expected {len(_FILES)} files transferred, "
            f"got file_count={result.ref.file_count}"
        )

        # 3b. LOCATION — the real bucket holds exactly the expected data keys
        #     under the destination prefix (no strays, no misplacement).
        #     transfer.upload also writes a `{key}.sha256` sidecar per file
        #     (part of the egress contract), so compare the data keys and assert
        #     each sidecar landed alongside.
        expected_keys = {f"{dst_prefix}/{rel}" for rel in _FILES}
        landed_keys = await _list_keys(store, dst_prefix)
        data_keys = {k for k in landed_keys if not k.endswith(".sha256")}
        assert data_keys == expected_keys, (
            f"assets landed at the wrong location.\n"
            f"  expected: {sorted(expected_keys)}\n"
            f"  actual:   {sorted(data_keys)}"
        )
        assert all(f"{k}.sha256" in landed_keys for k in expected_keys), (
            f"missing sha256 sidecar(s); landed: {sorted(landed_keys)}"
        )

        # 3c. INTEGRITY — each landed object is byte-for-byte the source.
        for rel, data in _FILES.items():
            assert await cs.get_bytes(f"{dst_prefix}/{rel}") == data
    finally:
        # Runs on any path (incl. assertion failure) so reruns against the
        # persistent real bucket stay idempotent.
        import contextlib

        for rel in _FILES:
            for prefix in (src_prefix, dst_prefix):
                for key in (f"{prefix}/{rel}", f"{prefix}/{rel}.sha256"):
                    with contextlib.suppress(Exception):
                        await ops.delete(key, store=store)


@pytest.mark.s3_integration
async def test_s3_sdr_atlan_upload_lands_assets(tmp_path):
    """SDR customer→Atlan directory handoff against real S3 (keyless)."""
    write_dapr_component(
        tmp_path / "components",
        name="s3-sdr-store",
        binding_type="bindings.aws.s3",
        metadata={"bucket": S3_BUCKET, "region": S3_REGION},
    )
    store = create_store_from_binding(
        "s3-sdr-store", components_dir=tmp_path / "components"
    )
    await _assert_sdr_handoff_lands_assets(store, provider="s3")


@pytest.mark.azure_integration
async def test_azure_sdr_atlan_upload_lands_assets(tmp_path):
    """SDR customer→Atlan directory handoff against real Azure Blob (WIF)."""
    write_dapr_component(
        tmp_path / "components",
        name="azure-sdr-store",
        binding_type="bindings.azure.blobstorage",
        metadata={
            "accountName": AZURE_ACCOUNT,
            "containerName": AZURE_CONTAINER,
            "azureClientId": AZURE_CLIENT_ID,
            "azureTenantId": AZURE_TENANT_ID,
            "federatedTokenFile": AZURE_FEDERATED_TOKEN_FILE,
        },
    )
    store = create_store_from_binding(
        "azure-sdr-store", components_dir=tmp_path / "components"
    )
    # CloudStore's Azure provider name is "adls" (see storage/cloud.py).
    await _assert_sdr_handoff_lands_assets(store, provider="adls")


@pytest.mark.gcs_integration
async def test_gcs_sdr_atlan_upload_lands_assets(tmp_path):
    """SDR customer→Atlan directory handoff against real GCS (ADC / WIF)."""
    write_dapr_component(
        tmp_path / "components",
        name="gcs-sdr-store",
        binding_type="bindings.gcp.bucket",
        metadata={"bucket": GCS_BUCKET, "project_id": GCS_PROJECT_ID},
    )
    store = create_store_from_binding(
        "gcs-sdr-store", components_dir=tmp_path / "components"
    )
    await _assert_sdr_handoff_lands_assets(store, provider="gcs")
