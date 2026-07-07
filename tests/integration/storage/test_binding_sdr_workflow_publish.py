"""Generalized SDR workflow → real-cloud-bucket publish validation.

Runs a **real** (embedded-Temporal) extraction workflow whose activity produces
asset-shaped records via the SDK's real ``RollingFileWriter``, then persists the
extracted output to the **real** provisioned cloud bucket (S3 / Azure / GCS,
keyless OIDC) via the **production** ``persist_file_refs`` path — the same
FileReference→objectstore upload a connector activity performs on return
(run-scoped egress prefix + SHA-256 sidecar) — and asserts the assets land with
the correct **count** and **location**.

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

from application_sdk.contracts.types import FileReference, StorageTier
from application_sdk.storage import ops
from application_sdk.storage.binding import create_store_from_binding
from application_sdk.storage.cloud import CloudStore
from application_sdk.storage.factory import create_local_store
from application_sdk.storage.file_ref_sync import persist_file_refs
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


def _count_records(paths: list) -> int:
    """Sum the record counts across a set of JSON record files (sync helper so
    the blocking file reads stay out of the async test bodies)."""
    total = 0
    for path in paths:
        with open(path) as fh:
            total += len(json.load(fh))
    return total


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
    async with (
        await WorkflowEnvironment.start_local() as env,
        Worker(
            env.client,
            task_queue=f"sdr-extract-{qid}",
            workflows=[SdrExtractWorkflow],
            activities=[extract_assets],
        ),
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
    """Run the extraction workflow, persist its output to the real bucket via the
    production ``persist_file_refs`` path, and assert the assets land with the
    correct COUNT and LOCATION."""
    out_dir = await _run_extract_workflow(str(tmp_path))
    run = os.environ.get("GITHUB_RUN_ID", "local")
    run_prefix = f"integ-sdr-wf/{provider}-{run}-{uuid.uuid4().hex[:8]}"
    cs = CloudStore(store, provider=provider)
    landed: set[str] = set()
    try:
        # Production persist path: a real activity returns a FileReference for its
        # extracted output; the SDK's persist_file_refs uploads it to the
        # objectstore under the run-scoped egress prefix + writes a sha256
        # sidecar. Drive that exact code against the real bucket.
        ref = FileReference.from_local(out_dir, tier=StorageTier.RETAINED)
        result = await persist_file_refs(store, {"output": ref}, output_path=run_prefix)
        durable = result["output"]
        assert durable.is_durable, "ref not marked durable after persist"
        assert durable.storage_path and durable.storage_path.startswith(run_prefix), (
            f"persisted to an unexpected egress path: {durable.storage_path}"
        )

        # LOCATION — assets landed in the real bucket under the run prefix, with
        # a sha256 sidecar (the production egress contract).
        async for batch in obstore.list(store, prefix=run_prefix):
            for item in batch:
                landed.add(str(item["path"]))
        assert landed, f"no assets persisted under {run_prefix} in the real bucket"
        assert all(k.startswith(run_prefix + "/") for k in landed), (
            f"asset(s) landed outside the run prefix: {sorted(landed)}"
        )
        assert any(k.endswith(".sha256") for k in landed), (
            "no sha256 sidecar written to the bucket"
        )

        # COUNT — read the persisted records back from the REAL bucket and assert
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
            ), "persisted asset records are not nested under the connection QN"
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
    assert _count_records([os.path.join(out_dir, f) for f in files]) == _N_RECORDS


@pytest.mark.integration
async def test_workflow_output_persists_via_file_ref_locally(tmp_path):
    """The production persist path — FileReference → ``persist_file_refs`` — is
    what a real activity uses to move extracted output into the objectstore
    (SHA-256 sidecars, run-scoped egress prefix). Validate it creds-free against
    a LocalStore: it's the exact code the per-cloud tests run against real
    buckets, so this pins the wiring on every SDK PR."""
    import glob

    out_dir = await _run_extract_workflow(str(tmp_path / "extract"))
    store_root = tmp_path / "objectstore"
    store = create_local_store(store_root)
    run_prefix = "artifacts/apps/sdr-generic/workflows/wf1/run1"

    ref = FileReference.from_local(out_dir, tier=StorageTier.RETAINED)
    result = await persist_file_refs(store, {"output": ref}, output_path=run_prefix)
    durable = result["output"]

    # LOCATION — the persisted ref is durable and lands under the run prefix.
    assert durable.is_durable, "ref not marked durable after persist"
    assert durable.storage_path, "durable ref has no storage_path"
    assert durable.storage_path.startswith(run_prefix), durable.storage_path

    landed = [
        p
        for p in glob.glob(
            os.path.join(str(store_root), run_prefix, "**", "*"), recursive=True
        )
        if os.path.isfile(p)
    ]
    record_files = [p for p in landed if p.endswith(".json")]
    assert record_files, f"no records persisted under {run_prefix}: {landed}"

    # COUNT — every extracted record persisted.
    assert _count_records(record_files) == _N_RECORDS

    # INTEGRITY — a sha256 sidecar was written to the store next to the output.
    assert any(p.endswith(".sha256") for p in landed), (
        f"no sha256 sidecar persisted under {run_prefix}: {landed}"
    )


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
