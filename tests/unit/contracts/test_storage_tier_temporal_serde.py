"""Integration test: StorageTier / FileReference round-trips through Temporal.

Uses ``WorkflowEnvironment.start_local()`` to spin up the embedded Temporal
dev server and run a minimal workflow that receives a ``FileReference`` as
input and echoes it back as output.  The test proves that all three
``StorageTier`` values survive JSON serialisation → Temporal payload →
JSON deserialisation intact.

Why this matters
----------------
``FileReference`` is a frozen dataclass that travels through Temporal's
2 MB payload system as JSON.  The ``tier`` field is a ``str`` enum
(``StorageTier``).  Temporal's ``DefaultPayloadConverter`` must:

1. Encode ``StorageTier.RETAINED`` → ``"retained"`` (the string value).
2. Decode ``"retained"`` back → ``StorageTier.RETAINED`` using the type
   annotation on the dataclass field.

This test catches any regression in that flow.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from temporalio import workflow
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.types import FileReference, StorageTier
from application_sdk.execution.sandbox import SandboxConfig

# ---------------------------------------------------------------------------
# Minimal workflow definition
# ---------------------------------------------------------------------------


@dataclass
class _EchoInput(Input, allow_unbounded_fields=True):
    """Workflow input carrying a single FileReference."""

    ref: FileReference = FileReference()


@dataclass
class _EchoOutput(Output, allow_unbounded_fields=True):
    """Workflow output echoing the same FileReference."""

    ref: FileReference = FileReference()


@workflow.defn(name="StorageTierEchoWorkflow")
class _StorageTierEchoWorkflow:
    """Minimal workflow: receive FileReference, echo it back unchanged."""

    @workflow.run
    async def run(self, input: _EchoInput) -> _EchoOutput:
        return _EchoOutput(ref=input.ref)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Use the same sandbox passthrough config as the production worker so the
# workflow module can import application_sdk without restriction.
_SANDBOX_RUNNER = SandboxedWorkflowRunner(
    restrictions=SandboxConfig().to_temporal_restrictions()
)


async def _run_echo(client: Client, ref: FileReference) -> FileReference:
    """Execute the echo workflow and return the FileReference from the output."""
    result: _EchoOutput = await client.execute_workflow(
        "StorageTierEchoWorkflow",
        _EchoInput(ref=ref),
        id=f"serde-test-{ref.tier.value}-{id(ref)}",
        task_queue="serde-test-queue",
        result_type=_EchoOutput,
    )
    return result.ref


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tier, storage_path",
    [
        (StorageTier.TRANSIENT, "file_refs/abc123.parquet"),
        (
            StorageTier.RETAINED,
            "artifacts/apps/myapp/workflows/wf1/run1/file_refs/abc.parquet",
        ),
        (
            StorageTier.PERSISTENT,
            "persistent-artifacts/apps/myapp/file_refs/abc.parquet",
        ),
    ],
    ids=["transient", "retained", "persistent"],
)
async def test_storage_tier_survives_temporal_round_trip(
    tier: StorageTier, storage_path: str
) -> None:
    """FileReference.tier is preserved after Temporal payload serialisation."""
    original = FileReference(
        local_path="/tmp/test.parquet",
        storage_path=storage_path,
        is_durable=True,
        file_count=3,
        tier=tier,
    )

    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(
            env.client,
            task_queue="serde-test-queue",
            workflows=[_StorageTierEchoWorkflow],
            workflow_runner=_SANDBOX_RUNNER,
        ):
            echoed = await _run_echo(env.client, original)

    assert (
        echoed.tier == tier
    ), f"Expected tier={tier!r} but got {echoed.tier!r} after Temporal round-trip"
    assert echoed.storage_path == storage_path
    assert echoed.is_durable is True
    assert echoed.file_count == 3


@pytest.mark.asyncio
async def test_default_tier_is_transient_after_round_trip() -> None:
    """FileReference without explicit tier defaults to TRANSIENT through serde."""
    original = FileReference(storage_path="file_refs/default.parquet", is_durable=True)
    assert original.tier == StorageTier.TRANSIENT  # sanity check

    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(
            env.client,
            task_queue="serde-test-queue",
            workflows=[_StorageTierEchoWorkflow],
            workflow_runner=_SANDBOX_RUNNER,
        ):
            echoed = await _run_echo(env.client, original)

    assert echoed.tier == StorageTier.TRANSIENT


@pytest.mark.asyncio
async def test_tier_string_value_is_lowercase() -> None:
    """StorageTier serialises to its lowercase string value (not the enum name)."""
    assert StorageTier.TRANSIENT.value == "transient"
    assert StorageTier.RETAINED.value == "retained"
    assert StorageTier.PERSISTENT.value == "persistent"

    # Reconstruct from value — this is what Temporal's converter does.
    assert StorageTier("transient") is StorageTier.TRANSIENT
    assert StorageTier("retained") is StorageTier.RETAINED
    assert StorageTier("persistent") is StorageTier.PERSISTENT
