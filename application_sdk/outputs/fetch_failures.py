"""Fetch-failure capture: one SDK path for an item the app could not fetch.

An app that fetches items in parallel (one request per report, per page, per
dashboard) records each *expected* per-item failure where it happens, inside
the per-item coroutine, and lets everything else propagate::

    from application_sdk.outputs import FetchFailureReason, record_fetch_failure

    async def fetch_report(report_id: str) -> Report | None:
        try:
            return await client.get_report(report_id)
        except ReportNotFound as exc:  # typed and expected: record it here
            record_fetch_failure(
                FetchFailureReason.NOT_FOUND,
                entity_type="Report",
                record_id=report_id,
                endpoint="/reports/{id}",
                http_status=404,
                detail=str(exc),
            )
            return None

    async with asyncio.TaskGroup() as tg:  # anything else fails the activity
        tasks = [tg.create_task(fetch_report(r)) for r in report_ids]

This is the sibling of :func:`~application_sdk.outputs.skip_record`, for a
different question. ``skip_record`` keeps a record the app *received* and chose
not to emit; a fetch failure has no record, only the id of what is missing, so
its reasons describe the source (:class:`FetchFailureReason`), not the data's
shape.

:func:`record_fetch_failure` is a plain function, safe to call from the event
loop, from ``asyncio.gather`` / ``TaskGroup`` child tasks, and from a sync
function run through ``run_in_thread`` — all of which see the activity's sink
through the copied context. A line holds ids only, never source data, so the
activity's lines are kept in memory (bounded by :data:`MAX_SKIPPED_RECORDS`)
and written once when the activity returns.

When the activity returns, the output interceptor:

* adds ``fetch-failures`` and ``fetch-failures-<reason>`` to the activity's
  metrics (here, on the event loop, never from a worker thread);
* uploads the activity's lines as a part under
  ``{run prefix}/fetch-failures/parts/``.

After ``run()`` returns, the framework task ``App.merge_fetch_failures``
concatenates the parts into ``{run prefix}/fetch-failures.ndjson``, adds it as
the run artifact ``fetch-failures``, and the workflow result is marked
:attr:`~application_sdk.contracts.base.OutputStatus.PARTIAL_SUCCESS`.

A run that records nothing produces none of this. An activity that raises
records nothing either: its failures are dropped with it, and the retry that
re-runs the body records its own.

Bounding is ADR-0020's two tiers, as for skipped records: the counts are
unbounded; the kept lines and the per-failure warnings are capped by
:data:`MAX_SKIPPED_RECORDS`. Every failure lands in exactly one of: a line in
the merged file, ``fetch-failures-truncated``, or ``fetch-failures-unpersisted``.

The SDK applies no failure threshold. An app that loses *every* item has an
outage, not a partial run, and should raise; only the app knows its
denominator. See ``docs/concepts/apps.md``.
"""

from __future__ import annotations

import contextvars
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final

import orjson

from application_sdk._logging import get_logger
from application_sdk.outputs.collector import get_outputs
from application_sdk.outputs.models import Metric
from application_sdk.outputs.skipped import (
    MAX_SKIPPED_RECORDS,
    RunLogLayout,
    activity_part_stem,
)

__all__ = [
    "current_fetch_failures",
    "FETCH_FAILURES_ARTIFACT",
    "FETCH_FAILURES_FILE",
    "FETCH_FAILURES_LAYOUT",
    "FETCH_FAILURES_METRIC",
    "FETCH_FAILURES_PARTS_DIR",
    "FETCH_FAILURES_TRUNCATED_METRIC",
    "FETCH_FAILURES_UNPERSISTED_METRIC",
    "FetchFailureReason",
    "FetchFailureSink",
    "FetchFailureSnapshot",
    "persist_activity_fetch_failures",
    "reason_metric_name",
    "record_fetch_failure",
]


_logger = get_logger(__name__)

FETCH_FAILURES_METRIC: Final = "fetch-failures"
FETCH_FAILURES_TRUNCATED_METRIC: Final = "fetch-failures-truncated"
FETCH_FAILURES_UNPERSISTED_METRIC: Final = "fetch-failures-unpersisted"
FETCH_FAILURES_ARTIFACT: Final = "fetch-failures"

#: Object-store layout under the run prefix (``build_output_path()``).
FETCH_FAILURES_PARTS_DIR: Final = "fetch-failures/parts"
FETCH_FAILURES_FILE: Final = "fetch-failures.ndjson"

FETCH_FAILURES_LAYOUT: Final = RunLogLayout(
    parts_dir=FETCH_FAILURES_PARTS_DIR,
    file_name=FETCH_FAILURES_FILE,
    artifact=FETCH_FAILURES_ARTIFACT,
    display_name="Fetch failures",
)


class FetchFailureReason(StrEnum):
    """Why an item could not be fetched. A closed set, so failures aggregate across the fleet.

    Adding a member is an SDK change: pick the closest existing reason and put
    the specifics in ``detail``, which lands in the file only.

    Attributes:
        SOURCE_UNAVAILABLE: The source failed to serve the item — a 5xx, a
            timeout, a dropped connection — after the app's own retries.
        NOT_FOUND: The item was listed but is gone when fetched (404, or a
            deleted object).
        FORBIDDEN: The credential may not read the item (401/403, or a
            source-side permission error).
        RATE_LIMITED: The source kept throttling the request (429) past the
            app's retry budget.
    """

    SOURCE_UNAVAILABLE = "source_unavailable"
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    RATE_LIMITED = "rate_limited"


def reason_metric_name(reason: FetchFailureReason) -> str:
    """The per-reason metric name, e.g. ``fetch-failures-not-found``."""
    return f"{FETCH_FAILURES_METRIC}-{reason.value.replace('_', '-')}"


@dataclass(frozen=True)
class FetchFailureSnapshot:
    """What one activity recorded, taken once when its sink is closed.

    Attributes:
        seen: Every failure recorded, including those past the cap.
        by_reason: ``seen`` split by reason.
        lines: The kept NDJSON lines; at most :data:`MAX_SKIPPED_RECORDS`.
    """

    seen: int
    by_reason: dict[FetchFailureReason, int]
    lines: tuple[bytes, ...]


@dataclass
class FetchFailureSink:
    """One activity's fetch failures, held in memory until the activity returns.

    Thread-safe: a sync function run through ``run_in_thread`` records into the
    same sink as the event loop. The output interceptor installs a fresh sink
    per activity and persists it when the activity returns. A test that drives
    an activity body directly can install its own with
    ``current_fetch_failures.set(FetchFailureSink())``.
    """

    _seen: int = 0
    _by_reason: dict[FetchFailureReason, int] = field(default_factory=dict)
    _lines: list[bytes] = field(default_factory=list)
    _closed: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, reason: FetchFailureReason, line: bytes) -> int | None:
        """Count one failure and keep ``line`` while under the cap.

        Returns the activity's running count, or ``None`` when the sink was
        already closed (the activity returned before the call landed).
        """
        with self._lock:
            if self._closed:
                return None
            self._seen += 1
            self._by_reason[reason] = self._by_reason.get(reason, 0) + 1
            if len(self._lines) < MAX_SKIPPED_RECORDS:
                self._lines.append(line)
            return self._seen

    @property
    def seen(self) -> int:
        with self._lock:
            return self._seen

    def close(self) -> FetchFailureSnapshot:
        """Stop accepting failures and return what was recorded."""
        with self._lock:
            self._closed = True
            return FetchFailureSnapshot(
                seen=self._seen,
                by_reason=dict(self._by_reason),
                lines=tuple(self._lines),
            )


current_fetch_failures: contextvars.ContextVar[FetchFailureSink | None] = (
    contextvars.ContextVar("fetch_failures", default=None)
)


def record_fetch_failure(
    reason: FetchFailureReason,
    *,
    entity_type: str,
    record_id: str,
    endpoint: str | None = None,
    http_status: int | None = None,
    detail: str | None = None,
) -> None:
    """Record that one item could not be fetched, keeping its id for review after the run.

    Call from inside a ``@task`` (activity), at the point the per-item fetch
    failed — in the per-item coroutine, or in a sync function run through
    ``run_in_thread``. Record only typed, expected failures; let anything else
    propagate so the activity fails and Temporal retries it. See the module
    docstring for what the SDK does with the failure when the activity returns.

    Args:
        reason: Why the fetch failed, from the closed :class:`FetchFailureReason` set.
        entity_type: The entity the item would have become, e.g. ``"Report"``.
        record_id: A non-sensitive identifier for the item (a source key or
            name), used in the warning log and the file.
        endpoint: The endpoint or operation that failed, as a template
            (``/reports/{id}``) rather than a URL with ids or query strings.
            Written to the file only.
        http_status: The HTTP status the source returned, when there was one.
        detail: Optional specifics for the reviewer. Written to the file only.

    Raises:
        TypeError: If ``reason`` is not a :class:`FetchFailureReason`.
    """
    if not isinstance(reason, FetchFailureReason):
        raise TypeError(
            f"reason must be a FetchFailureReason, got {type(reason).__name__}; "
            "free-string reasons cannot be aggregated across the fleet"
        )
    sink = current_fetch_failures.get()
    if sink is None:
        # Outside the output interceptor (an app test calling its own code
        # directly). Inside an activity the interceptor installs one up front,
        # so every child task and thread shares it.
        sink = FetchFailureSink()
        current_fetch_failures.set(sink)

    line = orjson.dumps(
        {
            "record_id": record_id,
            "entity_type": entity_type,
            "reason": reason.value,
            "endpoint": endpoint,
            "http_status": http_status,
            "detail": detail,
        },
        option=orjson.OPT_APPEND_NEWLINE,
    )
    seen = sink.add(reason, line)

    if seen is None:
        _logger.warning(
            "Fetch failure for %s (%s) recorded after its activity returned; "
            "it is not counted",
            record_id,
            entity_type,
        )
    elif seen <= MAX_SKIPPED_RECORDS:
        _logger.warning(
            "Failed to fetch %s (%s): %s",
            record_id,
            entity_type,
            reason.value,
        )
    elif seen == MAX_SKIPPED_RECORDS + 1:
        _logger.warning(
            "More than %d fetch failures in this activity; further failures are "
            "counted but neither logged nor kept",
            MAX_SKIPPED_RECORDS,
        )


def _part_location() -> tuple[Path, str]:
    """The local file and part name for the current activity's failures.

    The local file is per attempt; the part name is per activity, so a retry's
    upload replaces an earlier attempt's part rather than adding to it.
    """
    from temporalio import activity  # noqa: PLC0415 — lazy: workflow sandbox

    if activity.in_activity():
        from application_sdk.execution._temporal.activity_utils import (  # noqa: PLC0415 — circular: execution imports app, which imports outputs
            run_temp_dir,
        )

        info = activity.info()
        name = activity_part_stem(info.activity_id)
        return (
            Path(run_temp_dir())
            / "fetch-failures"
            / f"{name}-attempt-{info.attempt}.ndjson",
            f"{name}.ndjson",
        )
    name = f"fetch-failures-{uuid.uuid4().hex}.ndjson"
    return Path(tempfile.gettempdir()) / name, name


def _write_lines(path: Path, lines: tuple[bytes, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(lines))


async def persist_activity_fetch_failures(sink: FetchFailureSink) -> None:
    """Close the activity's sink: add its metrics and upload its part. Fails open.

    Called by the output interceptor when the activity returns. The counts
    always land as metrics; a write or upload failure is logged and recorded
    as ``fetch-failures-unpersisted`` (the lines that part held).
    """
    snapshot = sink.close()
    if snapshot.seen == 0:
        return

    outputs = get_outputs()
    outputs.add_metric(Metric(name=FETCH_FAILURES_METRIC, value=snapshot.seen))
    for reason, count in snapshot.by_reason.items():
        outputs.add_metric(Metric(name=reason_metric_name(reason), value=count))

    kept = len(snapshot.lines)
    try:
        from application_sdk._runtime.offload import (  # noqa: PLC0415 — keep the workflow sandbox import set small
            run_in_thread,
        )
        from application_sdk.execution._temporal.activity_utils import (  # noqa: PLC0415 — circular: execution imports app, which imports outputs
            build_output_path,
        )
        from application_sdk.storage import (  # noqa: PLC0415 — lazy: heavy
            upload_file,
        )

        path, part_name = _part_location()
        await run_in_thread(_write_lines, path, snapshot.lines)
        key = f"{build_output_path()}/{FETCH_FAILURES_PARTS_DIR}/{part_name}"
        await upload_file(key, path)
    # conformance: ignore[E004] fail-open by design (FND-5294): logged at ERROR and counted in fetch-failures-unpersisted
    except Exception:
        # conformance: ignore[L022] the persist failed and these failures are lost from the run's file; fail-open keeps the run going (FND-5294)
        _logger.error(
            "Failed to persist %d fetch failure(s); they are counted but "
            "missing from the run's fetch-failures file",
            kept,
            exc_info=True,
        )
        outputs.add_metric(Metric(name=FETCH_FAILURES_UNPERSISTED_METRIC, value=kept))
