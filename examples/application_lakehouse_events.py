"""AE-triggered lakehouse ingestion example using events_read + events_ack.

Demonstrates the recommended shape for an app that consumes events from
Atlan's Automation Engine via the SDK's lakehouse module:

    event-ingestion-app  ─►  AE event-consumer-node  ─►  this workflow

When AE detects unprocessed rows in its events Iceberg table
(``automation_engine.<events_table>``), it triggers this workflow with the
table name. The workflow uses ``events_read`` to read pending events,
dispatches them to a ``handler``, and publishes the AE-expected
Parquet ack via ``events_ack``.

Key components:

- ``LakehouseEventApp``        — v3 SDK App subclass with two ``@task``
                                methods (``handle_events`` + ``write_ack``)
- ``events_read``              — fetches events from the lakehouse and
                                dispatches to ``handler``; guarantees
                                ``results`` aligns 1:1 with ``events``
- ``events_ack``               — publishes the Parquet ack at the
                                AE-consumed path layout

Required env vars (Polaris-vended, marketplace-injected in production):

    ICEBERG_CATALOG_URI       Polaris REST endpoint
    ICEBERG_CLIENT_ID         OAuth2 client id
    ICEBERG_CLIENT_SECRET     OAuth2 client secret
    ICEBERG_WAREHOUSE         Catalog warehouse (default: context_store)
    CLOUD                     aws | gcp | azure (default: aws)

Install the lakehouse extra:

    pip install atlan-application-sdk[lakehouse]

Run locally::

    # In one shell — Temporal + Dapr
    uv run poe start-deps

    # In another — the worker + handler
    ICEBERG_CATALOG_URI=http://localhost:8181/api/catalog \\
    ICEBERG_CLIENT_ID=<id> ICEBERG_CLIENT_SECRET=<secret> \\
    uv run python examples/application_lakehouse_events.py

    # Trigger via the SDK handler with the AE payload shape:
    #   {"iceberg_table_name": "<your_events_table>"}
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from temporalio import workflow

from application_sdk.app import App, task
from application_sdk.contracts.base import Input, Output
from application_sdk.lakehouse import EventResult, events_ack, events_read
from application_sdk.main import AppConfig, run_combined_mode

_APP_NAME = "lakehouse-events-example"
_WORKFLOW_NAME = "ingestion"


# ---------------------------------------------------------------------------
# Workflow input — matches AE's event-consumer-node trigger payload
# ---------------------------------------------------------------------------


class IngestionInput(Input):
    """Payload AE's event-consumer-node passes when triggering this workflow."""

    iceberg_table_name: str = ""
    events_namespace: str = "automation_engine"
    workflow_id: str = ""
    triggered_by: str = ""
    trigger_id: str = ""
    trigger_name: str = ""
    event_cleanup_max_retries: int = 5
    event_cleanup_ack_source: str = ""


class IngestionOutput(Output):
    processed: int = 0
    success: int = 0
    failed: int = 0
    ack_path: str = ""


# ---------------------------------------------------------------------------
# Task-level I/O contracts
# ---------------------------------------------------------------------------


class HandleEventsInput(Input):
    events_namespace: str = ""
    events_table: str = ""


class HandleEventsOutput(Output, allow_unbounded_fields=True):
    events: list[Any] = []
    results: list[Any] = []  # serialised EventResult dicts


class WriteAckInput(Input, allow_unbounded_fields=True):
    events: list[Any] = []
    results: list[Any] = []
    workflow_run_id: str = ""


class WriteAckOutput(Output):
    ack_path: str = ""


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


class LakehouseEventApp(App):
    """Minimal AE-triggered ingestion app using the SDK lakehouse module."""

    name = _APP_NAME
    version = "0.1.0"
    description = "Example: AE-triggered ingestion via events_read + events_ack."

    @task(timeout_seconds=600, heartbeat_timeout_seconds=60)
    async def handle_events(self, input: HandleEventsInput) -> HandleEventsOutput:
        """Read pending events and dispatch each to ``_handler``.

        ``events_read`` guarantees the returned ``results`` list aligns
        1:1 with ``events`` — RETRY-on-exception, RETRY-on-count-mismatch,
        no-events-skip semantics are handled by the SDK.
        """

        async def _handler(events: list[dict[str, Any]]) -> list[EventResult]:
            # Replace this with your real per-event business logic.
            return [EventResult(status="SUCCESS") for _ in events]

        events, results = await events_read(
            namespace=input.events_namespace,
            table=input.events_table,
            handler=_handler,
            where="status = 'unprocessed'",
            sort_by="received_at",
        )

        # Serialise EventResult so the Temporal payload stays JSON-safe.
        result_dicts = [
            {"status": r.status, "error_message": r.error_message} for r in results
        ]
        return HandleEventsOutput(events=events, results=result_dicts)

    @task(timeout_seconds=120)
    async def write_ack(self, input: WriteAckInput) -> WriteAckOutput:
        """Publish the AE-expected Parquet ack."""
        if not input.events:
            return WriteAckOutput()

        results = [
            EventResult(
                status=r["status"],  # type: ignore[arg-type]
                error_message=r.get("error_message"),
            )
            for r in input.results
        ]
        path = await events_ack(
            input.events,
            results,
            app_name=_APP_NAME,
            workflow_name=_WORKFLOW_NAME,
            workflow_run_id=input.workflow_run_id,
        )
        return WriteAckOutput(ack_path=path)

    async def run(self, input: IngestionInput) -> IngestionOutput:  # type: ignore[override]
        if not input.iceberg_table_name:
            workflow.logger.error(
                "No iceberg_table_name in trigger payload — clean exit"
            )
            return IngestionOutput()

        run_id = workflow.info().run_id

        handled = await self.handle_events(
            HandleEventsInput(
                events_namespace=input.events_namespace,
                events_table=input.iceberg_table_name,
            )
        )
        events = handled.events
        results = handled.results
        if not events:
            return IngestionOutput()

        ack = await self.write_ack(
            WriteAckInput(events=events, results=results, workflow_run_id=run_id)
        )

        success = sum(1 for r in results if r.get("status") == "SUCCESS")
        failed = sum(1 for r in results if r.get("status") == "FAILED")
        return IngestionOutput(
            processed=len(events),
            success=success,
            failed=failed,
            ack_path=ack.ack_path,
        )


# ---------------------------------------------------------------------------
# Local-dev entrypoint
# ---------------------------------------------------------------------------


async def main() -> None:
    os.environ.setdefault("ATLAN_APPLICATION_NAME", _APP_NAME)
    os.environ.setdefault("ATLAN_LOCAL_DEVELOPMENT", "true")

    config = AppConfig(
        mode="combined",
        app_module=f"{__name__}:LakehouseEventApp",
        task_queue=f"{_APP_NAME}-queue",
        handler_host="127.0.0.1",
        handler_port=int(os.environ.get("ATLAN_APP_HTTP_PORT", "8000")),
    )
    await run_combined_mode(config)


if __name__ == "__main__":
    asyncio.run(main())
