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

What this earns its keep doing
------------------------------

Strip out the RETRY semantics and the consumer is three lines an app could
inline (``LakehouseReader.fetch_records`` + ``await process_fn(events)`` +
build the results list). The value-add is the dispatch wrapper around
``process_fn`` that guarantees the returned ``results`` list always aligns
1:1 with ``events``:

* Process function raises  → every event in the batch marked RETRY.
* Process function returns the wrong number of results  → same.
* No events read           → return ``([], [])`` without invoking process_fn.

If those guarantees aren't valuable for your shape, prefer ``LakehouseReader``
directly.

Known limits of the callable-injection pattern
----------------------------------------------

The class deliberately keeps the contract minimal — ``process_fn(events) →
results``. That's a good fit for stateless per-batch work but reveals
seams when:

* **Resource lifecycle.** The consumer doesn't run setup/teardown hooks for
  resources ``process_fn`` needs (DB connections, HTTP clients, model
  caches). Closures capture them, but lifecycle is then coupled to the call
  site by indentation.

* **Per-batch context.** ``process_fn`` receives only the events list. If
  it needs ``run_id`` / ``trigger_id`` / ``snapshot_id`` for tagging or
  observability, capture them in a closure or pull from
  ``temporalio.workflow.info()``.

* **Stack traces.** When ``process_fn`` raises, the frame chain is
  ``EventsConsumer.handle_events  →  process_fn``. Errors look like they
  originate inside the SDK; expect to read one frame deeper for the real
  failure site.

* **Composition.** Multi-stage per-event work (embed → store → enrich →
  publish) lives inside one ``process_fn`` body — the consumer doesn't
  pipeline. If your shape is genuinely multi-stage, build the pipeline in
  app code and have ``process_fn`` invoke it.

If multiple apps grow into these limits, the planned v2 design is an
async-iterator pattern (``async for batch in consumer.batches(table):``)
with explicit per-batch ack — that pattern hands control back to the app,
makes lifecycle a context-manager concern, and exposes per-batch metadata.
Not in v1 scope.
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

    See the module docstring for what this earns its keep doing and the
    known limits of the callable-injection pattern.
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
        where: str | None = None,
        limit: int | None = None,
        sort_by: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[ProcessingResult]]:
        """Fetch pending events and dispatch them to the process function.

        Returns ``(events, results)`` where ``results[i]`` corresponds to
        ``events[i]``. If the process function raises, every event is
        marked RETRY. If it returns the wrong number of results, every
        event is also marked RETRY.

        ``where`` and ``sort_by`` default to ``None`` — the consumer makes
        no assumption about the events-table schema. The AE convention is
        ``where="status = 'unprocessed'"`` and ``sort_by="received_at"``;
        apps following that convention should pass them explicitly. Apps
        with a different shape pass their own.

        Example::

            from application_sdk.lakehouse import (
                EventsConsumer, ProcessingResult,
            )

            async def process(events: list[dict]) -> list[ProcessingResult]:
                out = []
                for evt in events:
                    try:
                        await my_business_logic(evt)
                        out.append(ProcessingResult(status="SUCCESS"))
                    except TransientError as exc:
                        out.append(ProcessingResult(
                            status="RETRY", error_message=str(exc)
                        ))
                    except Exception as exc:
                        out.append(ProcessingResult(
                            status="FAILED", error_message=str(exc)
                        ))
                return out

            consumer = EventsConsumer(process)
            events, results = await consumer.handle_events(
                "automation_engine", "reverse_sync_description",
                where="status = 'unprocessed'",
                sort_by="received_at",
            )
            # events: list[dict]   (the unprocessed events read from the table)
            # results: list[ProcessingResult]  (aligned 1:1 with events)
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
