"""Events-driven lakehouse read.

A thin orchestrator for the AE-triggered pattern: a Temporal workflow receives
a trigger (``{events_table}``), reads pending events from the lakehouse in
batches, and hands each batch to a handler. No polling — each invocation is
one shot.

The lakehouse is a blackbox to the caller: ``events_read`` builds its
``LakehouseReader`` from the standard ``ICEBERG_*`` environment variables on
each call. App developers do not pass catalog or credentials in.

Writing results back is the app's responsibility — call ``events_read`` from
a Temporal activity, then write the returned results via ``events_ack`` in
whatever shape the app needs.

Batching
--------

``events_read`` does a **single** scan — the stateless Iceberg reader has no
cursor, so re-fetching with the same filter would just re-read the same head
rows. ``max_events`` caps that scan; ``batch_size`` then chunks the already
-fetched set for handler dispatch. The combinations:

* ``max_events=None`` → scan returns the full filtered set;
  ``max_events=M`` → scan returns at most M (the oldest M when ``sort_by``
  is set, since the sort+slice happens in-process).
* ``batch_size=None`` → the handler is invoked once with everything fetched;
  ``batch_size=N`` → the handler is invoked once per N-event chunk of the
  fetched set (keeps each handler call within heartbeat/memory limits).

Returns an :class:`~application_sdk.lakehouse.models.EventsReadResult` whose
``events`` and ``results`` align 1:1 and cover every fetched event;
``complete`` is ``False`` when the run aborted early (see below).

What this earns its keep doing
------------------------------

Strip out the loop + RETRY semantics and ``events_read`` is half a dozen
lines an app could inline (``LakehouseReader.fetch_records`` per batch +
``await handler(batch)`` + accumulate). The value-add is the dispatch
wrapper around ``handler`` that guarantees the returned ``results`` list
always aligns 1:1 with ``events``:

* Handler raises in batch N → that batch's events are marked RETRY, the
  loop aborts, prior batches' results are returned as-is.
* Handler returns the wrong number of results → same.
* No events read → return ``([], [])`` without invoking the handler.

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
  ``temporalio.activity.info()``.

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

from application_sdk.lakehouse.models import EventResult, EventsReadResult
from application_sdk.lakehouse.reader import LakehouseReader

logger = logging.getLogger(__name__)

EventHandler = Callable[[list[dict[str, Any]]], Awaitable[list[EventResult]]]


def _aborted(
    done_events: list[dict[str, Any]],
    done_results: list[EventResult],
    remaining: list[dict[str, Any]],
    reason: str,
) -> EventsReadResult:
    """Build an aborted result: prior successes + every remaining (failed batch
    and not-yet-attempted) event marked RETRY, with ``complete=False``."""
    return EventsReadResult(
        events=[*done_events, *remaining],
        results=[
            *done_results,
            *(EventResult(status="RETRY", error_message=reason) for _ in remaining),
        ],
        complete=False,
    )


async def events_read(
    *,
    namespace: str,
    table: str,
    handler: EventHandler,
    where: str | None = None,
    sort_by: str | None = None,
    batch_size: int | None = None,
    max_events: int | None = None,
) -> EventsReadResult:
    """Fetch events in one scan and dispatch them to ``handler`` in chunks.

    Returns an :class:`~application_sdk.lakehouse.models.EventsReadResult`
    where ``results[i]`` corresponds to ``events[i]`` and both cover every
    fetched event. If the handler raises (or returns a mismatched count) for
    a chunk, that chunk **and** any not-yet-attempted events are marked
    ``RETRY`` and ``complete`` is set ``False`` — so the caller can ack the
    partials (letting AE re-trigger) yet still distinguish an abort from a
    clean run that merely contained per-event ``RETRY`` verdicts.

    See the module docstring for the ``batch_size`` / ``max_events``
    semantics and the known limits of the callable-injection pattern.

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

        result = await events_read(
            namespace="automation_engine",
            table="reverse_sync_description",
            handler=handler,
            where="status = 'unprocessed'",
            sort_by="received_at",
            batch_size=1000,
            max_events=5000,
        )
        # result.events:   list[dict]          (every fetched event)
        # result.results:  list[EventResult]   (aligned 1:1 with events)
        # result.complete: bool                (False if the run aborted)
    """
    reader = LakehouseReader.from_env()

    # Single scan. The stateless Iceberg reader has no cursor, so re-fetching
    # with the same filter would re-read the same head rows — ``batch_size``
    # only chunks this already-fetched set for handler dispatch, and
    # ``max_events`` caps the scan itself.
    events = reader.fetch_records(
        namespace,
        table,
        where=where,
        sort_by=sort_by,
        limit=max_events,
    )
    if not events:
        return EventsReadResult([], [], complete=True)

    step = batch_size if batch_size and batch_size > 0 else len(events)

    all_events: list[dict[str, Any]] = []
    all_results: list[EventResult] = []

    for start in range(0, len(events), step):
        chunk = events[start : start + step]
        logger.info(
            "Processing %d events from %s.%s (total so far: %d/%d)",
            len(chunk),
            namespace,
            table,
            len(all_events) + len(chunk),
            len(events),
        )

        try:
            results = await handler(chunk)
        except Exception:
            logger.error(
                "Unhandled error in handler — marking the failed batch and "
                "%d not-yet-processed event(s) as RETRY and aborting",
                len(events) - start,
                exc_info=True,
            )
            return _aborted(
                all_events, all_results, events[start:], "batch processing failed"
            )

        if len(results) != len(chunk):
            logger.error(
                "handler returned %d results for %d events — marking the failed "
                "batch and remaining event(s) as RETRY and aborting",
                len(results),
                len(chunk),
            )
            return _aborted(
                all_events, all_results, events[start:], "result count mismatch"
            )

        all_events.extend(chunk)
        all_results.extend(results)

    return EventsReadResult(all_events, all_results, complete=True)
