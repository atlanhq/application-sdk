"""Event-triggered lakehouse consumer.

A thin orchestrator for the AE-triggered pattern: a Temporal workflow receives
a trigger (``{events_table}``), reads pending events from the lakehouse, and
hands them to a ``BatchProcessor``. No polling — each invocation is one shot,
driven by the upstream trigger.

Writing results is the app's responsibility — call ``handle_trigger`` from a
Temporal activity, then write the returned results via ``LakehouseWriter`` in
whatever shape the app needs.
"""

from __future__ import annotations

import logging
from typing import Any

from application_sdk.lakehouse.interface import LakehouseInterface
from application_sdk.lakehouse.models import ProcessingResult
from application_sdk.lakehouse.protocols import BatchProcessor

logger = logging.getLogger(__name__)


class EventTriggeredConsumer:
    """Single-shot fetch → process orchestrator driven by an upstream trigger."""

    def __init__(
        self,
        interface: LakehouseInterface,
        processor: BatchProcessor,
    ) -> None:
        self._interface = interface
        self._processor = processor

    async def handle_trigger(
        self,
        events_namespace: str,
        events_table: str,
        row_filter: str | None = "status = 'unprocessed'",
        limit: int | None = None,
        sort_by: str | None = "received_at",
    ) -> tuple[list[dict[str, Any]], list[ProcessingResult]]:
        """Fetch pending events and dispatch them to the processor.

        Returns ``(events, results)`` where ``results[i]`` corresponds to
        ``events[i]``. If the processor raises, every event is marked RETRY.
        If the processor returns the wrong number of results, every event is
        also marked RETRY — matches the strictness of the prior poll loop.
        """
        events = self._interface.reader.fetch_records(
            events_namespace,
            events_table,
            row_filter=row_filter,
            limit=limit,
            sort_by=sort_by,
        )
        if not events:
            logger.info("No pending events for %s.%s", events_namespace, events_table)
            return [], []

        logger.info(
            "Processing %d events from %s.%s",
            len(events),
            events_namespace,
            events_table,
        )

        await self._processor.setup()
        try:
            try:
                results = await self._processor.process_batch(events)
            except Exception:
                logger.error(
                    "Unhandled error in process_batch — marking all %d events as RETRY",
                    len(events),
                    exc_info=True,
                )
                results = [
                    ProcessingResult(
                        status="RETRY", error_message="batch processing failed"
                    )
                    for _ in events
                ]
        finally:
            await self._processor.teardown()

        if len(results) != len(events):
            logger.error(
                "process_batch returned %d results for %d events — marking all as RETRY",
                len(results),
                len(events),
            )
            results = [
                ProcessingResult(status="RETRY", error_message="result count mismatch")
                for _ in events
            ]

        return events, results
