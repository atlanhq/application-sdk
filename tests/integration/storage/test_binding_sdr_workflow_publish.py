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

from application_sdk.constants import WORKFLOW_OUTPUT_PATH_TEMPLATE
from application_sdk.contracts.base import PublishInputMixin
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

    # A small SQL-shaped crawl nested under the connection QN — varied typeNames
    # (1 db, 1 schema, 2 tables, rest columns) so the count/location assertions
    # exercise a realistic hierarchy rather than N identical records.
    schema_qn = f"{connection_qn}/db/schema"
    records = [
        {"typeName": "Database", "qualifiedName": f"{connection_qn}/db"},
        {"typeName": "Schema", "qualifiedName": schema_qn},
        {"typeName": "Table", "qualifiedName": f"{schema_qn}/table_0"},
        {"typeName": "Table", "qualifiedName": f"{schema_qn}/table_1"},
    ]
    records += [
        {"typeName": "Column", "qualifiedName": f"{schema_qn}/table_0/col_{i}"}
        for i in range(n - len(records))
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
        assert durable.storage_path and durable.storage_path.startswith(
            run_prefix
        ), f"persisted to an unexpected egress path: {durable.storage_path}"

        # LOCATION — assets landed in the real bucket under the run prefix, with
        # a sha256 sidecar (the production egress contract).
        async for batch in obstore.list(store, prefix=run_prefix):
            for item in batch:
                landed.add(str(item["path"]))
        assert landed, f"no assets persisted under {run_prefix} in the real bucket"
        assert all(
            k.startswith(run_prefix + "/") for k in landed
        ), f"asset(s) landed outside the run prefix: {sorted(landed)}"
        assert any(
            k.endswith(".sha256") for k in landed
        ), "no sha256 sidecar written to the bucket"

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
        assert (
            total == _N_RECORDS
        ), f"expected {_N_RECORDS} asset records in the bucket, got {total}"
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
    assert any(
        p.endswith(".sha256") for p in landed
    ), f"no sha256 sidecar persisted under {run_prefix}: {landed}"


async def _read_records_from_store(store, prefix: str) -> list[dict]:
    """Read every JSON record file under *prefix* the way the Publish app's
    input reader does — list the prefix, fetch each object's bytes, flatten the
    record lists. The ``.sha256`` integrity sidecars are skipped."""
    records: list[dict] = []
    async for batch in obstore.list(store, prefix=prefix):
        for item in batch:
            key = str(item["path"])
            if not key.endswith(".json"):
                continue
            result = await obstore.get_async(store, key)
            records.extend(json.loads(bytes(await result.bytes_async())))
    return records


@pytest.mark.integration
async def test_publish_reads_back_exactly_what_extract_wrote(tmp_path):
    """Two-app round trip, creds-free: EXTRACT writes asset records through the
    SDK writer and persists them to the object store under its ``output_path``'s
    ``transformed/`` prefix; the PUBLISH app then independently derives that same
    prefix from the extract output — via ``PublishInputMixin.transformed_data_prefix``
    (``WORKFLOW_OUTPUT_PATH_TEMPLATE`` + ``/transformed``) — and reads it back.
    Asserts the records Publish reads equal what Extract wrote (count + content).

    This pins the publish↔extract egress contract at the SDK level: a divergence
    between the prefix Extract writes to and the prefix Publish derives (the class
    of bug that lands 0 published assets) fails here, creds-free, on every SDK PR —
    complementing the unit-contract test and the connector two-store e2e guards."""
    # EXTRACT side: run the workflow, then persist its output to the store under
    # the run's output_path + /transformed — the exact prefix Publish will read.
    out_dir = await _run_extract_workflow(str(tmp_path / "extract"))
    store = create_local_store(tmp_path / "objectstore")

    output_path = WORKFLOW_OUTPUT_PATH_TEMPLATE.format(
        application_name="sdr-generic",
        workflow_id="wf-roundtrip",
        run_id="run-roundtrip",
    )
    transformed_prefix = f"{output_path}/transformed"
    ref = FileReference.from_local(out_dir, tier=StorageTier.RETAINED)
    await persist_file_refs(store, {"output": ref}, output_path=transformed_prefix)

    # PUBLISH side: derive the input prefix the way the Publish app does — from
    # the extract output_path via PublishInputMixin — and confirm it resolves to
    # exactly where Extract persisted the transformed records.
    publish_input = PublishInputMixin(output_path=output_path)
    assert (
        publish_input.transformed_data_prefix == transformed_prefix
    ), "Publish derived a different transformed prefix than Extract wrote to"

    # What EXTRACT wrote (source of truth: the local writer output).
    written: list[dict] = []
    for name in os.listdir(out_dir):
        if name.endswith(".json"):
            with open(os.path.join(out_dir, name)) as fh:
                written.extend(json.load(fh))

    # What PUBLISH reads (from the store at the derived prefix).
    read_back = await _read_records_from_store(
        store, publish_input.transformed_data_prefix
    )

    # COUNT — every extracted record is visible to Publish.
    assert len(read_back) == _N_RECORDS, (
        f"Publish read {len(read_back)} records at "
        f"{publish_input.transformed_data_prefix}, expected {_N_RECORDS}"
    )
    assert len(read_back) == len(written)
    # CONTENT — Publish reads exactly the records Extract wrote (order-independent).
    assert sorted(read_back, key=lambda r: r["qualifiedName"]) == sorted(
        written, key=lambda r: r["qualifiedName"]
    ), "records Publish read back differ from what Extract wrote"


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


# ---------------------------------------------------------------------------
# Emulator (MinIO) — the CHEAP, creds-free counterpart of the real-cloud tests.
# Runs in the SDK storage-emulator job on every SDK PR (no real-cloud cost).
# NOTE: this is an application-sdk test, so it does NOT run on connector PRs —
# the sdr-e2e action runs the connector's OWN SDR suite (its inputs.test-path),
# and that suite + the two-store guards give the connector its workflow →
# publish → count/location coverage. The real-cloud variants above run nightly.
# ---------------------------------------------------------------------------

_MINIO_ENDPOINT = os.environ.get("AWS_ENDPOINT_URL", "http://localhost:9000")
_MINIO_USER = os.environ.get("MINIO_ROOT_USER", "minioadmin")
_MINIO_PASS = os.environ.get("MINIO_ROOT_PASSWORD", "minioadmin")
_MINIO_BUCKET = os.environ.get("ATLAN_OBJECT_STORE_BUCKET", "sdk-atlan-objectstore")


def _minio_reachable() -> bool:
    import httpx

    try:
        with httpx.Client(timeout=3.0) as client:
            return client.get(f"{_MINIO_ENDPOINT}/minio/health/live").status_code < 500
    except Exception:
        return False


@pytest.mark.storage_emulator
async def test_emulator_sdr_workflow_publish_lands_assets(tmp_path):
    """Generalized workflow → persist_file_refs → count/location against MinIO.

    Same connector-agnostic harness as the real-cloud tests, on an emulator, so
    it can run on every connector PR without real-cloud creds or cost."""
    if not _minio_reachable():
        pytest.skip(f"MinIO not reachable at {_MINIO_ENDPOINT}")
    write_dapr_component(
        tmp_path / "components",
        name="minio-sdr-wf-store",
        binding_type="bindings.aws.s3",
        metadata={
            "bucket": _MINIO_BUCKET,
            "region": "us-east-1",
            "endpoint": _MINIO_ENDPOINT,
            "forcePathStyle": "true",
            "accessKey": _MINIO_USER,
            "secretKey": _MINIO_PASS,
        },
    )
    store = create_store_from_binding(
        "minio-sdr-wf-store", components_dir=tmp_path / "components"
    )
    await _assert_workflow_publish_lands_assets(store, "s3", tmp_path)
