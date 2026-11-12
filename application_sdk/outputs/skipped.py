"""Skipped-record capture: one SDK path for a record an app skips on purpose.

An app that decides not to emit a record — for example a record whose
qualifiedName input is blank, which would otherwise publish a broken identity —
calls :func:`skip_record` from inside its per-record loop::

    from application_sdk.outputs import SkipReason, skip_record

    if not schema_name:
        await skip_record(
            row,
            SkipReason.BLANK_IDENTITY,
            record_id=table_id,
            entity_type="Table",
            detail="schema name is blank",
        )
        continue

A synchronous mapper cannot await :func:`skip_record`; it raises a
:class:`RecordSkipped` leaf such as :class:`BlankIdentity` and the loop records
it (see :class:`RecordSkipped`).

Each call:

* appends one NDJSON line ``{record_id, entity_type, reason, detail, record}``
  to the activity's local skipped-records file (written off the event loop);
* adds ``skipped-records`` and ``skipped-records-<reason>`` to the run metrics;
* logs one warning naming only ``record_id``, ``entity_type`` and ``reason`` —
  never the record, which is customer source data.

The SDK does the rest. At activity end the output interceptor uploads the
activity's file as a *part* under ``{run prefix}/skipped-records/parts/``.
After ``run()`` returns, the framework task ``App.merge_skipped_records``
concatenates the parts into one ``{run prefix}/skipped-records.ndjson`` and
adds it as the run artifact ``skipped-records``, and the workflow result is
marked :attr:`~application_sdk.contracts.base.OutputStatus.PARTIAL_SUCCESS`.

A run that skips nothing produces none of this: no file, no artifact, no
``skipped-records`` metric, and its status is untouched. The presence of the
artifact is itself the signal that there is something to review.

Bounding follows ADR-0020's two tiers. The counts are unbounded — every skip is
counted. The two output surfaces, the lines in the merged file and the
per-record warnings, are capped by one constant,
:data:`MAX_SKIPPED_RECORDS`. Every skip lands in exactly one of: a line in the
file, ``skipped-records-truncated`` (dropped by the cap), or
``skipped-records-unpersisted`` (lost to a storage failure, which fails open).
"""

from __future__ import annotations

import asyncio
import contextvars
import re
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import ClassVar, Final

import orjson
from pydantic import BaseModel

from application_sdk._logging import get_logger
from application_sdk.outputs.models import Metric

_logger = get_logger(__name__)

#: Caps both output surfaces of the skipped-record path: the lines kept in the
#: run's merged file, and the per-record warnings each activity logs. One
#: constant, so the file and the logs can never disagree about what was cut.
#: The ``skipped-records`` count is never capped.
MAX_SKIPPED_RECORDS: Final = 10_000

SKIPPED_RECORDS_METRIC: Final = "skipped-records"
SKIPPED_RECORDS_TRUNCATED_METRIC: Final = "skipped-records-truncated"
SKIPPED_RECORDS_UNPERSISTED_METRIC: Final = "skipped-records-unpersisted"
SKIPPED_RECORDS_ARTIFACT: Final = "skipped-records"

#: Object-store layout under the run prefix (``build_output_path()``).
SKIPPED_RECORDS_PARTS_DIR: Final = "skipped-records/parts"
SKIPPED_RECORDS_FILE: Final = "skipped-records.ndjson"

# Buffered lines are written to the local file once they pass this size, so a
# burst of skips costs one thread hop per chunk rather than one per record.
_FLUSH_BYTES: Final = 64 * 1024
_UNSAFE_KEY_CHARS = re.compile(r"[^A-Za-z0-9._-]")


class SkipReason(StrEnum):
    """Why a record was skipped. A closed set, so skips aggregate across the fleet.

    Adding a member is an SDK change: pick the closest existing reason and put
    the specifics in ``detail``, which lands in the file only.

    Attributes:
        BLANK_IDENTITY: A field the record's identity (qualifiedName) is built
            from is blank — an empty string or ``None``. Emitting it would
            publish a broken identity such as ``{conn}//{schema}``.
        MISSING_REQUIRED_FIELD: A field the target entity requires, other than
            an identity input, is absent.
        MALFORMED_RECORD: The source record cannot be parsed or has the wrong
            shape for its type.
        UNSUPPORTED_TYPE: The source record is of a type the app does not map.
    """

    BLANK_IDENTITY = "blank_identity"
    MISSING_REQUIRED_FIELD = "missing_required_field"
    MALFORMED_RECORD = "malformed_record"
    UNSUPPORTED_TYPE = "unsupported_type"


SkippableRecord = Mapping[str, object] | BaseModel
"""A record :func:`skip_record` accepts: a mapping (a source row) or a pydantic model."""


class RecordSkipped(Exception):
    """A synchronous mapper's way to hand a skipped record up to its ``@task`` loop.

    :func:`skip_record` is async, so a plain ``def`` mapper cannot call it. The
    mapper raises a leaf of this class instead, carrying everything
    :func:`skip_record` needs; the loop catches it and records the skip::

        # mapper: stays sync and pure
        def map_table(row: Row, connection_qn: str) -> Table:
            if not row.schema_name:
                raise BlankIdentity(
                    row,
                    record_id=row.table_id,
                    entity_type="Table",
                    detail="schema name is blank",
                )
            return Table.creator(...)

        # @task loop
        for row in rows:
            try:
                asset = map_table(row, connection_qn)
            except RecordSkipped as skip:
                await skip_record(
                    skip.record,
                    skip.reason,
                    record_id=skip.record_id,
                    entity_type=skip.entity_type,
                    detail=skip.detail,
                )
                continue

    The loop catches only this class. A creator's ``ValueError`` for a value
    the mapper did not guard still fails the task, so an asset never vanishes
    without a guard naming why.

    Raise a leaf, never this base: the reason is the leaf's identity (a
    ClassVar, as ADR-0013 does for error codes), so it cannot differ per
    instance. There is one leaf per :class:`SkipReason`.

    Not an :class:`~application_sdk.errors.AppError`: a skip is a decision, not
    a failure, and has no category, owner or retry policy. One that escapes the
    ``@task`` uncaught fails the activity as a non-retryable ``InternalError``.

    The exception carries the record itself, not just its ids: a mapper may
    skip a nested record (one column of a table row, one path of a spec) that
    the loop never holds, and the file must keep the record that was actually
    skipped. The record is customer source data, so it is kept out of
    ``args``, ``str()`` and ``repr()`` — tracebacks, logs and Temporal failure
    text name only ``record_id``, ``entity_type`` and ``reason``.

    Attributes:
        record: The skipped source record, as :func:`skip_record` takes it.
        reason: Why the record was skipped. Fixed per leaf.
        record_id: A non-sensitive identifier for the record.
        entity_type: The entity the record would have become, e.g. ``"Table"``.
        detail: Optional specifics for the reviewer; reaches the file only.
    """

    reason: ClassVar[SkipReason]

    def __init__(
        self,
        record: SkippableRecord,
        *,
        record_id: str,
        entity_type: str,
        detail: str | None = None,
    ) -> None:
        if type(self) is RecordSkipped:
            raise TypeError(
                "raise a RecordSkipped leaf such as BlankIdentity; the reason is "
                "fixed by the class"
            )
        # ``args`` feeds repr() and traceback text: ids only, never the record.
        super().__init__(record_id, entity_type)
        self.record = record
        self.record_id = record_id
        self.entity_type = entity_type
        self.detail = detail

    def __str__(self) -> str:
        # ids and reason only: ``detail`` is for the file, not logs.
        return f"{self.entity_type} {self.record_id} skipped: {self.reason.value}"

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(record_id={self.record_id!r}, "
            f"entity_type={self.entity_type!r})"
        )

    def __reduce__(self) -> tuple[object, ...]:
        # Keyword-only __init__ breaks Exception's default pickling, which a
        # mapper run in a process pool depends on.
        return (
            _rebuild_record_skipped,
            (type(self), self.record, self.record_id, self.entity_type, self.detail),
        )


def _rebuild_record_skipped(
    cls: type[RecordSkipped],
    record: SkippableRecord,
    record_id: str,
    entity_type: str,
    detail: str | None,
) -> RecordSkipped:
    return cls(record, record_id=record_id, entity_type=entity_type, detail=detail)


class BlankIdentity(RecordSkipped):
    """A field the record's qualifiedName is built from is blank. See :class:`RecordSkipped`."""

    reason: ClassVar[SkipReason] = SkipReason.BLANK_IDENTITY


class MissingRequiredField(RecordSkipped):
    """A required field other than an identity input is absent. See :class:`RecordSkipped`."""

    reason: ClassVar[SkipReason] = SkipReason.MISSING_REQUIRED_FIELD


class MalformedRecord(RecordSkipped):
    """The source record cannot be parsed or has the wrong shape. See :class:`RecordSkipped`."""

    reason: ClassVar[SkipReason] = SkipReason.MALFORMED_RECORD


class UnsupportedType(RecordSkipped):
    """The source record is of a type the app does not map. See :class:`RecordSkipped`."""

    reason: ClassVar[SkipReason] = SkipReason.UNSUPPORTED_TYPE


def reason_metric_name(reason: SkipReason) -> str:
    """The per-reason metric name, e.g. ``skipped-records-blank-identity``."""
    return f"{SKIPPED_RECORDS_METRIC}-{reason.value.replace('_', '-')}"


class _RollbackFailedError(Exception):
    """A failed append could not be rolled back; the file may end mid-line."""


def _append_lines(path: Path, chunk: bytes) -> None:
    """Append ``chunk`` whole, or leave the file exactly as it was.

    A write can fail part-way (``ENOSPC`` after some bytes landed). The file is
    then truncated back to its size before the append, so it never ends in a
    partial NDJSON line. If that truncate fails too, :class:`_RollbackFailedError`
    tells the caller the file can no longer be trusted.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as fh:
        start = fh.tell()
        try:
            fh.write(chunk)
            fh.flush()
        except BaseException:
            try:
                fh.truncate(start)
            except OSError as exc:
                raise _RollbackFailedError(str(path)) from exc
            raise


@dataclass
class SkippedRecordSink:
    """One activity's skipped records, buffered and written to a local NDJSON file.

    The output interceptor installs a fresh sink per activity (lazily, on the
    first :func:`skip_record`) and uploads :attr:`path` when the activity
    returns. A test that drives an activity body directly can install its own
    with ``current_skipped_records.set(SkippedRecordSink(path))``.

    Attributes:
        path: Local file the kept lines are appended to. Unique per activity
            *attempt*, so a retry never appends to a failed attempt's lines.
        part_name: Object name of the uploaded part; one per activity, so a
            retry's upload replaces any earlier one. Defaults to ``path.name``.
        seen: Every skip recorded through this sink.
        kept: Lines written (or buffered) to :attr:`path`; at most
            :data:`MAX_SKIPPED_RECORDS`. Lines lost to a failed local write
            are moved from here to ``skipped-records-unpersisted``.
    """

    path: Path
    part_name: str = ""
    seen: int = 0
    kept: int = 0
    _buffer: list[bytes] = field(default_factory=list)
    _buffered_bytes: int = 0
    # Set when a failed append could not be rolled back: the file may end
    # mid-line, so nothing more is written and nothing is uploaded.
    _poisoned: bool = False
    # Child tasks of one activity share the sink. Appends and their rollbacks
    # run one at a time, or a rollback could truncate another task's rows.
    _write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def add(self, line: bytes | None) -> None:
        """Count a skip and keep ``line`` when under the cap (``None`` = over the cap)."""
        self.seen += 1
        if line is None:
            return
        if self._poisoned:
            _count_unpersisted(1)
            return
        self.kept += 1
        self._buffer.append(line)
        self._buffered_bytes += len(line)
        if self._buffered_bytes >= _FLUSH_BYTES:
            await self.flush()

    @property
    def object_name(self) -> str:
        return self.part_name or self.path.name

    @property
    def has_room(self) -> bool:
        return self.kept < MAX_SKIPPED_RECORDS

    async def flush(self) -> None:
        """Write buffered lines to :attr:`path` off the event loop. Fails open.

        A write failure (e.g. a full scratch disk) never fails the activity:
        it is logged, and the chunk's lines are counted as
        ``skipped-records-unpersisted`` instead of kept.
        """
        async with self._write_lock:
            await self._flush_locked()

    async def _flush_locked(self) -> None:
        if not self._buffer or self._poisoned:
            return
        from application_sdk._runtime.offload import (  # noqa: PLC0415 — keep the workflow sandbox import set small
            run_in_thread,
        )

        lines = len(self._buffer)
        chunk = b"".join(self._buffer)
        self._buffer.clear()
        self._buffered_bytes = 0
        try:
            await run_in_thread(_append_lines, self.path, chunk)
        except _RollbackFailedError:
            # The file may end mid-line: drop all of it rather than ship
            # invalid NDJSON. Every line it held, and every later one, is
            # unpersisted.
            lost = self.kept
            self.kept = 0
            self._poisoned = True
            _logger.error(
                "Failed to write skipped records and could not roll the local "
                "file back; %d record(s) are counted but missing from the "
                "run's skipped-records file",
                lost,
                exc_info=True,
            )
            _count_unpersisted(lost)
        # conformance: ignore[E004] fail-open by design (FND-4881): logged at ERROR and counted in skipped-records-unpersisted
        except Exception:
            # _append_lines rolled the file back, so earlier lines stay valid.
            self.kept -= lines
            _logger.error(
                "Failed to write %d skipped record(s) to the local file; they "
                "are counted but missing from the run's skipped-records file",
                lines,
                exc_info=True,
            )
            _count_unpersisted(lines)


def _count_unpersisted(n: int) -> None:
    from application_sdk.outputs import (  # noqa: PLC0415 — circular: the package re-exports this module
        get_outputs,
    )

    get_outputs().add_metric(Metric(name=SKIPPED_RECORDS_UNPERSISTED_METRIC, value=n))


current_skipped_records: contextvars.ContextVar[SkippedRecordSink | None] = (
    contextvars.ContextVar("skipped_records", default=None)
)


def new_activity_sink() -> SkippedRecordSink | None:
    """A sink for the current activity, or ``None`` when its path is unavailable.

    The output interceptor installs this before the activity body runs, so
    skips recorded from child tasks (``asyncio.gather``) share one sink.
    """
    try:
        return _new_sink()
    # conformance: ignore[E004] a sink is created lazily on first skip instead; nothing is lost
    except Exception:
        _logger.debug("No run scratch directory for skipped records", exc_info=True)
        return None


def _new_sink() -> SkippedRecordSink:
    """A sink for the current activity, under the run's scratch directory.

    Outside an activity (an app test calling its own code directly) the file
    goes to a fresh temp directory and nothing uploads it.
    """
    from temporalio import activity  # noqa: PLC0415 — lazy: workflow sandbox

    if activity.in_activity():
        from application_sdk.execution._temporal.activity_utils import (  # noqa: PLC0415 — circular: execution imports app, which imports outputs
            run_temp_dir,
        )

        info = activity.info()
        name = _UNSAFE_KEY_CHARS.sub("_", info.activity_id)
        # The local file is per attempt: run_temp_dir() and the activity ID
        # are both stable across retries, and a failed attempt's file stays
        # behind. The part keeps the activity's name, so a retry's upload
        # replaces rather than adds to an earlier one.
        return SkippedRecordSink(
            Path(run_temp_dir())
            / "skipped-records"
            / f"{name}-attempt-{info.attempt}.ndjson",
            part_name=f"{name}.ndjson",
        )
    # No directory is created until the first flush writes to it.
    return SkippedRecordSink(
        Path(tempfile.gettempdir()) / f"skipped-records-{uuid.uuid4().hex}.ndjson"
    )


def _serialize(
    record: SkippableRecord,
    reason: SkipReason,
    record_id: str,
    entity_type: str,
    detail: str | None,
) -> bytes:
    payload: object = (
        record.model_dump(mode="json")
        if isinstance(record, BaseModel)
        else dict(record)
    )
    return orjson.dumps(
        {
            "record_id": record_id,
            "entity_type": entity_type,
            "reason": reason.value,
            "detail": detail,
            "record": payload,
        },
        # Source rows carry driver types orjson does not know (Decimal, bytes):
        # the file is for a person to read, so their string form is enough.
        default=str,
        option=orjson.OPT_APPEND_NEWLINE | orjson.OPT_NON_STR_KEYS,
    )


async def skip_record(
    record: SkippableRecord,
    reason: SkipReason,
    *,
    record_id: str,
    entity_type: str,
    detail: str | None = None,
) -> None:
    """Skip a record on purpose, keeping it for review after the run.

    Call from inside a ``@task`` (activity). See the module docstring for what
    the SDK does with the record at activity and run end.

    Args:
        record: The skipped source record. Written only to the tenant's object
            store, never to logs or telemetry.
        reason: Why it was skipped, from the closed :class:`SkipReason` set.
        record_id: A non-sensitive identifier for the record (a source key or
            name), used in the warning log and the file.
        entity_type: The entity the record would have become, e.g. ``"Table"``.
        detail: Optional specifics for the reviewer. Written to the file only.

    Raises:
        TypeError: If ``reason`` is not a :class:`SkipReason`.
    """
    if not isinstance(reason, SkipReason):
        raise TypeError(
            f"reason must be a SkipReason, got {type(reason).__name__}; "
            "free-string reasons cannot be aggregated across the fleet"
        )
    from application_sdk.outputs import (  # noqa: PLC0415 — circular: the package re-exports this module
        get_outputs,
    )

    sink = current_skipped_records.get()
    if sink is None:
        sink = _new_sink()
        current_skipped_records.set(sink)

    line = (
        _serialize(record, reason, record_id, entity_type, detail)
        if sink.has_room
        else None
    )
    await sink.add(line)

    outputs = get_outputs()
    outputs.add_metric(Metric(name=SKIPPED_RECORDS_METRIC, value=1))
    outputs.add_metric(Metric(name=reason_metric_name(reason), value=1))

    if sink.seen <= MAX_SKIPPED_RECORDS:
        _logger.warning(
            "Skipped record %s (%s): %s",
            record_id,
            entity_type,
            reason.value,
        )
    elif sink.seen == MAX_SKIPPED_RECORDS + 1:
        _logger.warning(
            "More than %d records skipped in this activity; further skips are "
            "counted but neither logged nor kept",
            MAX_SKIPPED_RECORDS,
        )


async def persist_activity_skips(sink: SkippedRecordSink) -> None:
    """Upload one activity's file as a part under the run prefix. Fails open.

    Called by the output interceptor when the activity returns. A failure is
    logged and recorded as ``skipped-records-unpersisted`` (the lines that
    part held) — the counts and the run status still land.
    """
    from application_sdk.outputs import get_outputs  # noqa: PLC0415 — circular

    # Fails open itself; a failed write moves its lines out of ``kept``.
    await sink.flush()
    if sink.kept == 0:
        return
    try:
        from application_sdk.execution._temporal.activity_utils import (  # noqa: PLC0415 — circular: execution imports app, which imports outputs
            build_output_path,
        )
        from application_sdk.storage import (  # noqa: PLC0415 — lazy: heavy
            upload_file,
        )

        key = f"{build_output_path()}/{SKIPPED_RECORDS_PARTS_DIR}/{sink.object_name}"
        await upload_file(key, sink.path)
    # conformance: ignore[E004] fail-open by design (FND-4881): logged at ERROR and counted in skipped-records-unpersisted
    except Exception:
        _logger.error(
            "Failed to persist %d skipped record(s); they are counted but "
            "missing from the run's skipped-records file",
            sink.kept,
            exc_info=True,
        )
        get_outputs().add_metric(
            Metric(name=SKIPPED_RECORDS_UNPERSISTED_METRIC, value=sink.kept)
        )


def _append_capped(part: Path, merged: Path, room: int) -> int:
    """Append up to ``room`` lines of ``part`` to ``merged``; return lines appended."""
    appended = 0
    with part.open("rb") as src, merged.open("ab") as dst:
        for line in src:
            if appended >= room:
                break
            dst.write(line)
            appended += 1
    return appended


@dataclass(frozen=True)
class MergeResult:
    """What :func:`merge_run_skips` produced."""

    key: str | None
    kept: int


async def merge_run_skips(run_prefix: str, scratch_dir: str) -> MergeResult:
    """Concatenate the run's parts into one capped file and register the artifact.

    Parts are merged in key order. At most :data:`MAX_SKIPPED_RECORDS` lines
    are kept. Returns ``key=None`` when there were no parts to merge, in which
    case no artifact is added.
    """
    from application_sdk._runtime.offload import (  # noqa: PLC0415 — keep the workflow sandbox import set small
        run_in_thread,
    )
    from application_sdk.outputs import get_outputs  # noqa: PLC0415 — circular
    from application_sdk.outputs.models import Artifact  # noqa: PLC0415
    from application_sdk.storage import (  # noqa: PLC0415 — lazy: heavy
        download_file,
        list_data_keys,
        upload_file,
    )

    parts = sorted(await list_data_keys(f"{run_prefix}/{SKIPPED_RECORDS_PARTS_DIR}/"))
    if not parts:
        return MergeResult(key=None, kept=0)

    work = Path(scratch_dir) / "skipped-records-merge"
    merged = work / SKIPPED_RECORDS_FILE
    await run_in_thread(work.mkdir, parents=True, exist_ok=True)
    await run_in_thread(merged.write_bytes, b"")  # truncate a retried attempt's file

    kept = 0
    for index, part_key in enumerate(parts):
        room = MAX_SKIPPED_RECORDS - kept
        if room <= 0:
            break
        local_part = work / f"part-{index}.ndjson"
        await download_file(part_key, local_part)
        kept += await run_in_thread(_append_capped, local_part, merged, room)

    key = f"{run_prefix}/{SKIPPED_RECORDS_FILE}"
    await upload_file(key, merged)
    get_outputs().add_artifact(
        Artifact(
            name=SKIPPED_RECORDS_ARTIFACT, path=key, display_name="Skipped records"
        )
    )
    return MergeResult(key=key, kept=kept)
