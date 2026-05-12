"""AE-triggered lakehouse event-processing example using events_read + events_ack.

Demonstrates the recommended shape for an app that consumes events from
Atlan's Automation Engine via the SDK's lakehouse module:

    event-ingestion-app  ─►  AE event-consumer-node  ─►  this workflow

When AE detects unprocessed rows in its per-trigger Iceberg table
(``apps.automation-engine.<workflow_slug>``), it triggers this workflow
with the table name. A single ``handle_events`` activity uses
``events_read`` to read pending events in batches of 1000 (capped at
5000 total), dispatches them to a ``handler``, then publishes the
AE-expected Parquet ack via ``events_ack``.

AE writes events with CloudEvent column names (``id``, ``data``,
``source``, ``topic``, ``time``, ...). ``events_ack`` keys the ack
Parquet by ``event_id``, so before publishing the ack we map AE's
``id`` column → the SDK's expected ``event_id`` key.

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

from temporalio import activity, workflow

from application_sdk.app import App, task
from application_sdk.contracts.base import Input, Output
from application_sdk.lakehouse import EventResult, events_ack, events_read
from application_sdk.main import AppConfig, run_combined_mode

_APP_NAME = "lakehouse-events-example"
_WORKFLOW_NAME = "process-events"
_BATCH_SIZE = 1000
_MAX_EVENTS = 5000


# ---------------------------------------------------------------------------
# Workflow input — matches AE's event-consumer-node trigger payload
# ---------------------------------------------------------------------------


class ProcessEventsInput(Input):
    """Payload AE's event-consumer-node passes when triggering this workflow."""

    iceberg_table_name: str = ""
    events_namespace: str = "apps.automation-engine"
    workflow_id: str = ""
    triggered_by: str = ""
    trigger_id: str = ""
    trigger_name: str = ""
    event_cleanup_max_retries: int = 5
    event_cleanup_ack_source: str = ""


class ProcessEventsOutput(Output):
    processed: int = 0
    success: int = 0
    failed: int = 0
    ack_path: str = ""


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


class LakehouseEventApp(App):
    """Minimal AE-triggered event-processing app using the SDK lakehouse module."""

    name = _APP_NAME
    version = "0.1.0"
    description = "Example: AE-triggered event processing via events_read + events_ack."

    @task(timeout_seconds=1800, heartbeat_timeout_seconds=60)
    async def handle_events(self, input: ProcessEventsInput) -> ProcessEventsOutput:
        """Read pending events in batches and publish the AE ack.

        ``events_read`` loops internally — fetching ``_BATCH_SIZE`` events
        per call, dispatching to ``_handler``, and stopping when the source
        is exhausted or ``_MAX_EVENTS`` total are processed. After the
        loop, ``events_ack`` writes a single Parquet ack covering every
        event processed.
        """

        async def _handler(events: list[dict[str, Any]]) -> list[EventResult]:
            # Each event is an AE CloudEvent row: keys include
            # ``id``, ``data`` (JSON-stringified), ``source``, ``topic``,
            # ``time``, ``received_at``, ``status``, ``retry_count``.
            # Replace this with your real per-event business logic.
            return [EventResult(status="SUCCESS") for _ in events]

        events, results = await events_read(
            namespace=input.events_namespace,
            table=input.iceberg_table_name,
            handler=_handler,
            where="status = 'unprocessed'",
            sort_by="received_at",
            batch_size=_BATCH_SIZE,
            max_events=_MAX_EVENTS,
        )
        if not events:
            return ProcessEventsOutput()

        # Map AE's CloudEvent ``id`` column to the ``event_id`` key that
        # ``events_ack`` writes into the ack Parquet. Until the SDK key
        # mismatch is resolved, every events_read→events_ack caller needs
        # this shim.
        events_for_ack = [{"event_id": e["id"]} for e in events]
        ack_path = await events_ack(
            events_for_ack,
            results,
            app_name=_APP_NAME,
            workflow_name=_WORKFLOW_NAME,
            workflow_run_id=activity.info().workflow_run_id,
        )

        success = sum(1 for r in results if r.status == "SUCCESS")
        failed = sum(1 for r in results if r.status == "FAILED")
        return ProcessEventsOutput(
            processed=len(events),
            success=success,
            failed=failed,
            ack_path=ack_path,
        )

    async def run(self, input: ProcessEventsInput) -> ProcessEventsOutput:  # type: ignore[override]
        if not input.iceberg_table_name:
            workflow.logger.error(
                "No iceberg_table_name in trigger payload — clean exit"
            )
            return ProcessEventsOutput()
        return await self.handle_events(input)


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
