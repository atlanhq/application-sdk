"""Events-driven lakehouse read.

A thin orchestrator for the AE-triggered pattern: a Temporal workflow receives
a trigger (``{events_table}``), reads pending events from the lakehouse, and
hands them to a handler. No polling — each invocation is one shot.

The lakehouse is a blackbox to the caller: ``events_read`` builds its
``LakehouseReader`` from the standard ``ICEBERG_*`` environment variables on
each call. App developers do not pass catalog or credentials in.

Writing results back is the app's responsibility — call ``events_read`` from
a Temporal activity, then write the returned results via ``events_ack`` in
whatever shape the app needs.

What this earns its keep doing
------------------------------

Strip out the RETRY semantics and ``events_read`` is three lines an app could
inline (``LakehouseReader.fetch_records`` + ``await handler(events)`` +
build the results list). The value-add is the dispatch wrapper around
``handler`` that guarantees the returned ``results`` list always aligns
1:1 with ``events``:

* Handler raises  → every event in the batch marked RETRY.
* Handler returns the wrong number of results  → same.
* No events read  → return ``([], [])`` without invoking the handler.

If those guarantees aren't valuable for your shape, prefer ``LakehouseReader``
directly.

Known limits of the callable-injection pattern
----------------------------------------------

The function deliberately keeps the contract minimal — ``handler(events) →
results``. That's a good fit for stateless per-batch work but reveals
seams when:

* **Resource lifecycle.** ``events_read`` doesn't run setup/teardown hooks
  for resources ``handler`` needs (DB connections, HTTP clients, model
  caches). Closures capture them, but lifecycle is then coupled to the call
  site by indentation.

* **Per-batch context.** ``handler`` receives only the events list. If
  it needs ``run_id`` / ``trigger_id`` / ``snapshot_id`` for tagging or
  observability, capture them in a closure or pull from
  ``temporalio.workflow.info()``.

* **Stack traces.** When ``handler`` raises, the frame chain is
  ``events_read  →  handler``. Errors look like they originate inside the
  SDK; expect to read one frame deeper for the real failure site.

* **Composition.** Multi-stage per-event work (embed → store → enrich →
  publish) lives inside one ``handler`` body — ``events_read`` doesn't
  pipeline. If your shape is genuinely multi-stage, build the pipeline in
  app code and have ``handler`` invoke it.

If multiple apps grow into these limits, the planned v2 design is an
async-iterator pattern (``async for batch in events_stream(table):``)
with explicit per-batch ack — that pattern hands control back to the app,
makes lifecycle a context-manager concern, and exposes per-batch metadata.
Not in v1 scope.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from application_sdk.lakehouse.models import EventResult
from application_sdk.lakehouse.reader import LakehouseReader

logger = logging.getLogger(__name__)

EventHandler = Callable[[list[dict[str, Any]]], Awaitable[list[EventResult]]]


async def events_read(
    *,
    namespace: str,
    table: str,
    handler: EventHandler,
    where: str | None = None,
    limit: int | None = None,
    sort_by: str | None = None,
) -> tuple[list[dict[str, Any]], list[EventResult]]:
    """Fetch pending events and dispatch them to ``handler``.

    Returns ``(events, results)`` where ``results[i]`` corresponds to
    ``events[i]``. If the handler raises, every event is marked RETRY. If
    it returns the wrong number of results, every event is also marked
    RETRY.

    ``where`` and ``sort_by`` default to ``None`` — ``events_read`` makes
    no assumption about the events-table schema. The AE convention is
    ``where="status = 'unprocessed'"`` and ``sort_by="received_at"``;
    apps following that convention should pass them explicitly. Apps
    with a different shape pass their own.

    Example::

        from application_sdk.lakehouse import events_read, EventResult

        async def handler(events: list[dict]) -> list[EventResult]:
            out = []
            for evt in events:
                try:
                    await my_business_logic(evt)
                    out.append(EventResult(status="SUCCESS"))
                except TransientError as exc:
                    out.append(EventResult(
                        status="RETRY", error_message=str(exc)
                    ))
                except Exception as exc:
                    out.append(EventResult(
                        status="FAILED", error_message=str(exc)
                    ))
            return out

        events, results = await events_read(
            namespace="automation_engine",
            table="reverse_sync_description",
            handler=handler,
            where="status = 'unprocessed'",
            sort_by="received_at",
        )
        # events: list[dict]   (the unprocessed events read from the table)
        # results: list[EventResult]  (aligned 1:1 with events)
    """
    reader = LakehouseReader.from_env()
    events = reader.fetch_records(
        namespace,
        table,
        where=where,
        limit=limit,
        sort_by=sort_by,
    )
    if not events:
        logger.info("No pending events for %s.%s", namespace, table)
        return [], []

    logger.info(
        "Processing %d events from %s.%s",
        len(events),
        namespace,
        table,
    )

    try:
        results = await handler(events)
    except Exception:
        logger.error(
            "Unhandled error in handler — marking all %d events as RETRY",
            len(events),
            exc_info=True,
        )
        results = [
            EventResult(status="RETRY", error_message="batch processing failed")
            for _ in events
        ]

    if len(results) != len(events):
        logger.error(
            "handler returned %d results for %d events — marking all as RETRY",
            len(results),
            len(events),
        )
        results = [
            EventResult(status="RETRY", error_message="result count mismatch")
            for _ in events
        ]

    return events, results
