"""Generalized SDR workflow → real-cloud-bucket publish validation.

Runs a **real** (embedded-Temporal) extraction workflow whose activity produces
asset-shaped records via the SDK's real ``RollingFileWriter``, then publishes the
extracted output directory to the **real** provisioned cloud bucket (S3 / Azure /
GCS, keyless OIDC) and asserts the assets land with the correct **count** and
**location**.

This is the connector-agnostic counterpart of the per-connector SDR extraction
(``BaseSDRIntegrationTest``): there is no real database or connector — a synthetic
extract activity stands in for connector-specific query logic (which each
connector's own SDR suite covers) — but it exercises the real
**workflow → SDK writer → cloud-bucket publish** path a customer's SDR run takes.

It closes the "real extraction → real bucket" half of DISTR-862 that
``test_binding_sdr_atlan_upload.py`` (upload-layer only: synthetic bytes, no
workflow) does not.

Layers:
* ``test_workflow_writes_asset_parquet_locally`` (marker: ``integration``) —
  validates the workflow + writer wiring with **no** cloud creds; runs in the
  SDK ``integration`` CI job and locally.
* ``test_{s3,azure,gcs}_sdr_workflow_publish_lands_assets`` — add the real-bucket
  publish + count/location assertions; skip without creds (autouse ``require_*``
  guards in conftest).
"""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from datetime import timedelta

import obstore
import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

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

# The connection an SDR extraction runs under. Asset qualifiedNames are nested
# under it, so we can assert both count (# records) and location (right bucket
# prefix + records under the connection QN).
_CONNECTION_QN = "default/sdr-generic/1730000000"
_N_RECORDS = 14  # a SQL-shaped crawl: 1 db + 1 schema + 2 tables + 10 columns


def _flush_json(batches: list, path: str) -> None:
    """RollingFileWriter flush callback — write buffered asset records as JSON.

    Each ``append`` hands a list of record dicts; ``batches`` is the list of
    those lists accumulated since the last rollover. Mirrors the connector SDR
    raw output shape (``raw/<entity>/records.json``)."""
    records = [rec for batch in batches for rec in batch]
    with open(path, "w") as fh:
        json.dump(records, fh)


@activity.defn
async def extract_assets(args: dict) -> str:
    """Synthetic SDR extraction: write asset-shaped records through the SDK's
    real ``RollingFileWriter``. Returns the writer's output directory (a local
    path this in-process test then publishes to the bucket)."""
    from application_sdk.storage.rolling import RollingFileWriter

    connection_qn = args["connection_qn"]
    n = int(args["n_records"])
    base_path = args["base_path"]

    records = [
        {
            "typeName": "Column",
            "qualifiedName": f"{connection_qn}/db/schema/table/col_{i}",
        }
        for i in range(n)
    ]

    async with RollingFileWriter(
        base_path=base_path,
        extension=".json",
        flush_fn=_flush_json,
        scoped_subdir_name="extracted",
    ) as writer:
        await writer.append(records)
    return writer.output_dir


@workflow.defn(sandboxed=False)
class SdrExtractWorkflow:
    """Minimal SDR extraction workflow — runs the extract activity."""

    @workflow.run
    async def run(self, args: dict) -> str:
        return await workflow.execute_activity(
            extract_assets,
            args,
            start_to_close_timeout=timedelta(seconds=120),
        )


async def _run_extract_workflow(base_path: str) -> str:
    """Run the extraction workflow on an embedded Temporal server; return the
    local output directory the activity wrote the asset parquet to."""
    qid = uuid.uuid4().hex[:8]
    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(
            env.client,
            task_queue=f"sdr-extract-{qid}",
            workflows=[SdrExtractWorkflow],
            activities=[extract_assets],
        ):
            return await env.client.execute_workflow(
                SdrExtractWorkflow.run,
                {
                    "base_path": base_path,
                    "connection_qn": _CONNECTION_QN,
                    "n_records": _N_RECORDS,
                },
                id=f"sdr-extract-{qid}",
                task_queue=f"sdr-extract-{qid}",
            )


async def _assert_workflow_publish_lands_assets(store, provider: str, tmp_path) -> None:
    """Run the extraction workflow, publish its output to the real bucket, and
    assert the assets land with the correct COUNT and LOCATION."""
    out_dir = await _run_extract_workflow(str(tmp_path))
    run = os.environ.get("GITHUB_RUN_ID", "local")
    dst_prefix = f"integ-sdr-wf/{provider}-{run}-{uuid.uuid4().hex[:8]}/published"
    cs = CloudStore(store, provider=provider)
    landed: set[str] = set()
    try:
        # Publish the extracted output directory → the real bucket (the SDR
        # customer-objectstore publish step).
        result = await transfer_upload(out_dir, dst_prefix, store=store)
        assert result.synced is True

        # LOCATION — assets landed in the real bucket under the publish prefix.
        async for batch in obstore.list(store, prefix=dst_prefix):
            for item in batch:
                landed.add(str(item["path"]))
        assert landed, f"no assets published under {dst_prefix} in the real bucket"
        assert all(k.startswith(dst_prefix + "/") for k in landed), (
            f"asset(s) landed outside the publish prefix: {sorted(landed)}"
        )

        # COUNT — read the published records back from the REAL bucket and assert
        # the asset-record count matches what the workflow extracted, and every
        # record is nested under the connection qualifiedName.
        total = 0
        for key in landed:
            if not key.endswith(".json"):
                continue
            records = json.loads(await cs.get_bytes(key))
            total += len(records)
            assert all(
                r["qualifiedName"].startswith(_CONNECTION_QN) for r in records
            ), "published asset records are not nested under the connection QN"
        assert total == _N_RECORDS, (
            f"expected {_N_RECORDS} asset records in the bucket, got {total}"
        )
    finally:
        for key in landed:
            with contextlib.suppress(Exception):
                await ops.delete(key, store=store)


# ---------------------------------------------------------------------------
# Creds-free wiring check — runs in the SDK `integration` CI job + locally
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_workflow_writes_asset_parquet_locally(tmp_path):
    """The extraction workflow runs and the SDK writer lands asset parquet
    locally with the right record count (no cloud creds needed)."""
    out_dir = await _run_extract_workflow(str(tmp_path))
    files = [f for f in os.listdir(out_dir) if f.endswith(".json")]
    assert files, f"no records written under {out_dir}"
    total = 0
    for f in files:
        with open(os.path.join(out_dir, f)) as fh:
            total += len(json.load(fh))
    assert total == _N_RECORDS


# ---------------------------------------------------------------------------
# Real-cloud publish + count/location — keyless OIDC, skip without creds
# ---------------------------------------------------------------------------


@pytest.mark.s3_integration
async def test_s3_sdr_workflow_publish_lands_assets(tmp_path):
    write_dapr_component(
        tmp_path / "components",
        name="s3-sdr-wf-store",
        binding_type="bindings.aws.s3",
        metadata={"bucket": S3_BUCKET, "region": S3_REGION},
    )
    store = create_store_from_binding(
        "s3-sdr-wf-store", components_dir=tmp_path / "components"
    )
    await _assert_workflow_publish_lands_assets(store, "s3", tmp_path)


@pytest.mark.azure_integration
async def test_azure_sdr_workflow_publish_lands_assets(tmp_path):
    write_dapr_component(
        tmp_path / "components",
        name="azure-sdr-wf-store",
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
        "azure-sdr-wf-store", components_dir=tmp_path / "components"
    )
    await _assert_workflow_publish_lands_assets(store, "adls", tmp_path)


@pytest.mark.gcs_integration
async def test_gcs_sdr_workflow_publish_lands_assets(tmp_path):
    write_dapr_component(
        tmp_path / "components",
        name="gcs-sdr-wf-store",
        binding_type="bindings.gcp.bucket",
        metadata={"bucket": GCS_BUCKET, "project_id": GCS_PROJECT_ID},
    )
    store = create_store_from_binding(
        "gcs-sdr-wf-store", components_dir=tmp_path / "components"
    )
    await _assert_workflow_publish_lands_assets(store, "gcs", tmp_path)
