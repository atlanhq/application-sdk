"""Integration test: StorageTier / FileReference round-trips through Temporal.

Runs a minimal echo workflow against the session's embedded Temporal dev
server (booted once by conftest.py via ``embedded_runtime``).  The test
proves that all three ``StorageTier`` values survive JSON serialisation →
Temporal payload → JSON deserialisation intact.

Why this matters
----------------
``FileReference`` is a Pydantic model that travels through Temporal's
2 MB payload system as JSON.  The ``tier`` field is a ``str`` enum
(``StorageTier``).  Temporal's ``pydantic_data_converter`` must:

1. Encode ``StorageTier.RETAINED`` → ``"retained"`` (the string value).
2. Decode ``"retained"`` back → ``StorageTier.RETAINED`` using the type
   annotation on the model field.

This test catches any regression in that flow.
"""

from __future__ import annotations

import pytest
from pydantic import Field
from temporalio import workflow
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.types import FileReference, StorageTier
from application_sdk.execution.sandbox import SandboxConfig

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Minimal workflow definition
# ---------------------------------------------------------------------------


class _EchoInput(Input, allow_unbounded_fields=True):
    """Workflow input carrying a single FileReference."""

    ref: FileReference = Field(default_factory=FileReference)


class _EchoOutput(Output, allow_unbounded_fields=True):
    """Workflow output echoing the same FileReference."""

    ref: FileReference = Field(default_factory=FileReference)


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
    temporal_client, task_queue, tier: StorageTier, storage_path: str
) -> None:
    """FileReference.tier is preserved after Temporal payload serialisation."""
    original = FileReference(
        local_path="/tmp/test.parquet",
        storage_path=storage_path,
        is_durable=True,
        file_count=3,
        tier=tier,
    )

    async with Worker(
        temporal_client,
        task_queue=task_queue,
        workflows=[_StorageTierEchoWorkflow],
        workflow_runner=_SANDBOX_RUNNER,
    ):
        result: _EchoOutput = await temporal_client.execute_workflow(
            "StorageTierEchoWorkflow",
            _EchoInput(ref=original),
            id=f"serde-test-{tier.value}",
            task_queue=task_queue,
            result_type=_EchoOutput,
        )

    assert (
        result.ref.tier == tier
    ), f"Expected tier={tier!r} but got {result.ref.tier!r} after Temporal round-trip"
    assert result.ref.storage_path == storage_path
    assert result.ref.is_durable is True
    assert result.ref.file_count == 3


@pytest.mark.asyncio
async def test_default_tier_is_transient_after_round_trip(
    temporal_client, task_queue
) -> None:
    """FileReference without explicit tier defaults to TRANSIENT through serde."""
    original = FileReference(storage_path="file_refs/default.parquet", is_durable=True)
    assert original.tier == StorageTier.TRANSIENT  # sanity check

    async with Worker(
        temporal_client,
        task_queue=task_queue,
        workflows=[_StorageTierEchoWorkflow],
        workflow_runner=_SANDBOX_RUNNER,
    ):
        result: _EchoOutput = await temporal_client.execute_workflow(
            "StorageTierEchoWorkflow",
            _EchoInput(ref=original),
            id="serde-test-default-tier",
            task_queue=task_queue,
            result_type=_EchoOutput,
        )

    assert result.ref.tier == StorageTier.TRANSIENT
