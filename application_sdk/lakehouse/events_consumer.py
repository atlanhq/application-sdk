"""Events-driven lakehouse consumer.

A thin orchestrator for the AE-triggered pattern: a Temporal workflow receives
a trigger (``{events_table}``), reads pending events from the lakehouse, and
hands them to a process function. No polling — each invocation is one shot.

The lakehouse is a blackbox to the consumer's caller: ``EventsConsumer`` takes
only an async ``process_fn`` at construction time and self-constructs its
``LakehouseReader`` from the standard ``ICEBERG_*`` environment variables on
first use. App developers do not pass catalog or credentials in.

Writing results back is the app's responsibility — call ``handle_events`` from
a Temporal activity, then write the returned results via ``LakehouseWriter``
in whatever shape the app needs.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from application_sdk.lakehouse.models import ProcessingResult
from application_sdk.lakehouse.reader import LakehouseReader

logger = logging.getLogger(__name__)

ProcessFn = Callable[[list[dict[str, Any]]], Awaitable[list[ProcessingResult]]]


class EventsConsumer:
    """Single-shot fetch → process orchestrator driven by an upstream trigger.

    Constructs its lakehouse reader from environment credentials lazily on
    first call to :meth:`handle_events` — apps don't pass a catalog in.
    """

    def __init__(self, process_fn: ProcessFn) -> None:
        self._process_fn = process_fn
        self._reader: LakehouseReader | None = None

    def _ensure_reader(self) -> LakehouseReader:
        if self._reader is None:
            self._reader = LakehouseReader.from_env()
        return self._reader

    async def handle_events(
        self,
        events_namespace: str,
        events_table: str,
        where: str | None = "status = 'unprocessed'",
        limit: int | None = None,
        sort_by: str | None = "received_at",
    ) -> tuple[list[dict[str, Any]], list[ProcessingResult]]:
        """Fetch pending events and dispatch them to the process function.

        Returns ``(events, results)`` where ``results[i]`` corresponds to
        ``events[i]``. If the process function raises, every event is
        marked RETRY. If it returns the wrong number of results, every
        event is also marked RETRY.
        """
        reader = self._ensure_reader()
        events = reader.fetch_records(
            events_namespace,
            events_table,
            where=where,
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

        try:
            results = await self._process_fn(events)
        except Exception:
            logger.error(
                "Unhandled error in process_fn — marking all %d events as RETRY",
                len(events),
                exc_info=True,
            )
            results = [
                ProcessingResult(
                    status="RETRY", error_message="batch processing failed"
                )
                for _ in events
            ]

        if len(results) != len(events):
            logger.error(
                "process_fn returned %d results for %d events — marking all as RETRY",
                len(results),
                len(events),
            )
            results = [
                ProcessingResult(status="RETRY", error_message="result count mismatch")
                for _ in events
            ]

        return events, results
