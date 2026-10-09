"""Read-only object-store assertion workflow for in-tenant e2e (FND-3571).

A system-app e2e suite runs on a CI runner that cannot see the tenant's object
store. Instead of getting store state *out*, the harness sends the suite's
expectations *in*: it appends one ``sdk:store-assert`` node to the DAG, on the
app's own task queue, so it is served by the same pod and the same store
binding the app used. The node evaluates each expectation and returns a
structured verdict as its output; the harness reads that output back from AE
and grades it.

Registered on every app worker, so worker start-up is identical on every
tenant, but gated at run time: unless ``ATLAN_STORE_ASSERT_ENABLED`` is set
(:data:`~application_sdk.constants.STORE_ASSERT_ENABLED`) the activity touches
nothing and returns ``enabled=False``. Only the e2e install sets it, per
tenant, through ``deploy.env_overrides``. Within an enabled run:

* Never PUT or DELETE. ``ABSENT``, ``PRESENT`` and ``COUNT`` only LIST (plus
  one HEAD for ``ABSENT``); ``RECORDS`` also GETs the parquet / JSONL files
  under its prefix, one at a time, within :data:`MAX_RECORD_FILES`,
  :data:`MAX_RECORD_FILE_BYTES` and :data:`MAX_RECORD_BYTES`.
* Prefixes must sit strictly below one of :data:`STORE_ASSERT_ROOTS`.
* Bounded: at most :data:`MAX_STORE_EXPECTATIONS` checks per run and at most
  :data:`MAX_KEYS_SCANNED` keys per prefix.
* The output carries counts and booleans, never keys or values.

Cross-cloud LIST semantics: obstore strips trailing slashes, and the SDK's
default listing view drops a zero-byte object only when it has children, so a
childless "folder" marker (GCS console, ADLS hierarchical namespace) is still
listed — and the marker *for* the prefix itself is never listed at all.
``ABSENT`` is therefore graded strictly — zero objects of any kind, markers
included, plus a HEAD on the bare prefix key, which is exactly what
``delete_prefix`` removes — while
``PRESENT`` and ``COUNT`` use the default ``list_keys`` view an app sees.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    import obstore

    from application_sdk import constants
    from application_sdk._runtime.offload import run_in_thread
    from application_sdk.contracts.base import Input, Output, SerializableEnum
    from application_sdk.contracts.types import MaxItems
    from application_sdk.execution._temporal import _store_records
    from application_sdk.observability.logger_adaptor import get_logger
    from application_sdk.storage.ops import (
        _drop_directory_markers,
        _is_local_dir_collision,
        _is_not_found,
        _resolve_store,
        normalize_key,
    )

logger = get_logger(__name__)

STORE_ASSERT_WORKFLOW_TYPE = "sdk:store-assert"
"""Temporal workflow type of the assertion node. Reserved: no app may claim it."""

STORE_ASSERT_ACTIVITY_NAME = "sdk:store-assert"
"""Activity the workflow runs; the only place the store is touched."""

STORE_ASSERT_ROOTS: tuple[str, ...] = (
    "artifacts/apps",
    "persistent-artifacts",
    "connection-cache",
)
"""Roots an expectation may look under. A prefix must name something strictly
below one of them, so no single check can enumerate a whole root."""

MAX_STORE_EXPECTATIONS = 20
"""Most checks one node evaluates."""

MAX_KEYS_SCANNED = 10_000
"""Most keys listed per prefix. A prefix holding more is reported as truncated
and a ``COUNT`` against it fails, rather than the scan running unbounded."""

# RECORDS checks can read up to MAX_RECORD_BYTES each; listing-only runs finish
# in seconds.
_START_TO_CLOSE = timedelta(minutes=20)
_RETRY = RetryPolicy(maximum_attempts=3, backoff_coefficient=2)


class StoreExpectationKind(SerializableEnum):
    """What a suite claims about a prefix."""

    ABSENT = "absent"
    """No object of any kind under the prefix, directory markers included."""
    PRESENT = "present"
    """At least one object in the default listing view."""
    COUNT = "count"
    """Exactly ``count`` objects in the default listing view."""
    RECORDS = "records"
    """A number of records, optionally only those matching conditions, across
    the parquet or JSONL files under the prefix."""


class RecordFormat(SerializableEnum):
    """File format a ``RECORDS`` check reads."""

    PARQUET = "parquet"
    """``*.parquet``. Needs pyarrow on the app's worker."""
    JSONL = "jsonl"
    """``*.jsonl``, ``*.ndjson`` and ``*.json``, one JSON object per line (the
    SDK's JSON writer)."""


RECORD_FILE_SUFFIXES: dict[RecordFormat, tuple[str, ...]] = {
    RecordFormat.PARQUET: (".parquet",),
    RecordFormat.JSONL: (".jsonl", ".ndjson", ".json"),
}
"""Keys under the prefix a ``RECORDS`` check reads; every other key is ignored."""


class FieldOp(SerializableEnum):
    """How a condition tests a field."""

    EQ = "eq"
    """Equals ``value``. Type-aware: ``true`` never equals ``1``."""
    IN = "in"
    """Equals one of ``values``."""
    PRESENT = "present"
    """Has a value: in the record, not null, not empty."""
    MISSING = "missing"
    """The opposite of ``PRESENT``."""


MAX_RECORD_CONDITIONS = 10
"""Most conditions on one ``RECORDS`` check."""

MAX_IN_VALUES = 20
"""Most values one ``IN`` condition lists."""

MAX_RECORD_FILES = 1_000
"""Most files one ``RECORDS`` check reads. More fails the check as ungradable."""

MAX_RECORD_FILE_BYTES = 128 * 1024 * 1024
"""Largest single file a ``RECORDS`` check reads; files are read one at a time,
so this bounds the node's memory."""

MAX_RECORD_BYTES = 512 * 1024 * 1024
"""Most bytes one ``RECORDS`` check reads across all its files."""

Scalar = str | int | float | bool
"""A value a condition compares against. Null is not one: use ``MISSING``."""


class FieldCondition(BaseModel):
    """One test on one field of a record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    field: str = Field(min_length=1, max_length=256)
    """Dotted path: ``type_name``, ``attributes.qualifiedName``. The first
    segment is a column or JSONL key, falling back to a Hive partition directory
    (``type_name=Table/``) when the file lacks it. Later segments walk structs,
    JSON objects, and string columns holding JSON."""
    op: FieldOp
    # Pydantic's default "smart" union keeps the exact JSON type: true stays a
    # bool and "3" stays a string, which equals() then compares strictly.
    value: Scalar | None = None
    """Required for ``EQ`` and rejected otherwise."""
    values: Annotated[list[Scalar], MaxItems(MAX_IN_VALUES)] | None = Field(
        default=None, min_length=1, max_length=MAX_IN_VALUES
    )
    """Required for ``IN`` and rejected otherwise."""

    @model_validator(mode="after")
    def _check_shape(self) -> FieldCondition:
        if any(not segment for segment in self.field.split(".")):
            raise ValueError(f"field {self.field!r} has an empty path segment")
        if (self.value is not None) != (self.op is FieldOp.EQ):
            raise ValueError("value is required for EQ and only valid there")
        if (self.values is not None) != (self.op is FieldOp.IN):
            raise ValueError("values is required for IN and only valid there")
        for v in [self.value, *(self.values or ())]:
            if isinstance(v, str) and len(v) > 1024:
                raise ValueError("a compared string is longer than 1024 characters")
        return self

    @property
    def path(self) -> tuple[str, ...]:
        return tuple(self.field.split("."))

    def holds(self, value: object) -> bool:
        """Whether *value* (or ``MISSING``) satisfies this condition."""
        if self.op is FieldOp.PRESENT:
            return _store_records.has_value(value)
        if self.op is FieldOp.MISSING:
            return not _store_records.has_value(value)
        if self.op is FieldOp.EQ:
            return _store_records.equals(value, self.value)
        return any(_store_records.equals(value, v) for v in self.values or ())


class _PrefixCheck(BaseModel):
    """Fields every check shares."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prefix: str = Field(max_length=1024)
    """Object-store key prefix, or a ``./local/tmp/...`` workflow path; both are
    normalised the same way every SDK storage call normalises them."""


class StoreAbsent(_PrefixCheck):
    """No object of any kind under the prefix, directory markers included."""

    kind: Literal[StoreExpectationKind.ABSENT] = StoreExpectationKind.ABSENT


class StorePresent(_PrefixCheck):
    """At least one object under the prefix, in the default listing view."""

    kind: Literal[StoreExpectationKind.PRESENT] = StoreExpectationKind.PRESENT


class StoreCount(_PrefixCheck):
    """Exactly ``count`` objects under the prefix, in the default listing view."""

    kind: Literal[StoreExpectationKind.COUNT] = StoreExpectationKind.COUNT
    count: int = Field(ge=0)


class StoreRecords(_PrefixCheck):
    """How many records — all, or those meeting every condition in ``where`` —
    the ``format`` files under the prefix hold.

    Exactly one of ``count`` (exactly) or ``at_least`` is required. "Every
    record has field X" is ``where=[X MISSING]`` with ``count=0``.
    """

    kind: Literal[StoreExpectationKind.RECORDS] = StoreExpectationKind.RECORDS
    format: RecordFormat
    where: Annotated[list[FieldCondition], MaxItems(MAX_RECORD_CONDITIONS)] = Field(
        default_factory=list, max_length=MAX_RECORD_CONDITIONS
    )
    count: int | None = Field(default=None, ge=0)
    at_least: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _one_bound(self) -> StoreRecords:
        if (self.count is None) == (self.at_least is None):
            raise ValueError("exactly one of count or at_least is required")
        return self


StoreExpectation = Annotated[
    StoreAbsent | StorePresent | StoreCount | StoreRecords,
    Field(discriminator="kind"),
]
"""One claim about one prefix: a model per kind, tagged by ``kind``, so a kind
added later never changes the shape of an existing one."""


class StoreObservation(BaseModel):
    """What the node saw under one prefix, and whether it met the claim."""

    prefix: str = ""
    kind: StoreExpectationKind = StoreExpectationKind.ABSENT
    expected_count: int | None = None
    passed: bool = False
    objects_all: int | None = None
    """Every listed object, directory markers included. ``None`` when the
    prefix was rejected or the listing failed."""
    objects_data: int | None = None
    """The default ``list_keys`` view: markers with children dropped. ``None``
    when not computed — an ``ABSENT`` check stops at the first object."""
    truncated: bool = False
    """The listing hit :data:`MAX_KEYS_SCANNED` before it ended."""
    expected_at_least: int | None = None
    """``RECORDS`` only: the lower bound, when the check gave one."""
    files_scanned: int | None = None
    """``RECORDS`` only: files of the check's format read in full."""
    records_scanned: int | None = None
    """``RECORDS`` only: rows across those files."""
    records_matched: int | None = None
    """``RECORDS`` only: rows meeting every condition (all rows when none)."""
    problem: str = ""
    """Why the check could not be evaluated, when it could not. Never carries
    store internals — a rejected prefix says which rule, a failed listing names
    the exception type."""


class StoreAssertInput(Input):
    """The expectations to evaluate, in the order to report them."""

    expectations: Annotated[
        list[StoreExpectation], MaxItems(MAX_STORE_EXPECTATIONS)
    ] = Field(default_factory=list, max_length=MAX_STORE_EXPECTATIONS)


class StoreAssertOutput(Output):
    """The verdict: one observation per expectation, in input order."""

    enabled: bool = False
    """Whether this deployment allows store assertions. When False nothing was
    read, ``observations`` is empty and ``passed`` is False."""
    passed: bool = False
    observations: Annotated[
        list[StoreObservation], MaxItems(MAX_STORE_EXPECTATIONS)
    ] = Field(default_factory=list, max_length=MAX_STORE_EXPECTATIONS)


def resolve_assert_prefix(prefix: str) -> str:
    """Normalise *prefix* and check it against the root allowlist.

    Args:
        prefix: As the suite wrote it.

    Returns:
        The listing prefix, with a trailing ``/`` so it cannot bleed into a
        sibling (``a/b`` must not match ``a/bc``).

    Raises:
        ValueError: The prefix escapes, is malformed, or is not strictly below
            an allowed root. The message names the rule, not the store.
    """
    key = normalize_key(prefix)
    segments = key.split("/")
    if not key or any(s in ("", ".", "..") for s in segments):
        raise ValueError(
            "prefix must be a plain relative key with no empty, '.' or '..' segments"
        )
    for root in STORE_ASSERT_ROOTS:
        root_segments = root.split("/")
        if segments[: len(root_segments)] == root_segments and len(segments) > len(
            root_segments
        ):
            return key + "/"
    raise ValueError(
        "prefix must sit strictly below one of: "
        + ", ".join(f"{r}/" for r in STORE_ASSERT_ROOTS)
    )


async def _scan(
    store: object, listing_prefix: str, *, limit: int
) -> tuple[list[tuple[str, int, str | None]], bool]:
    """List up to *limit* objects under *listing_prefix*.

    Returns:
        The objects read, and whether the listing had more than *limit*.
    """
    items: list[tuple[str, int, str | None]] = []
    async for batch in obstore.list(store, prefix=listing_prefix):  # type: ignore[arg-type]
        for item in batch:
            if len(items) >= limit:
                return items, True
            items.append((str(item["path"]), int(item["size"]), item.get("e_tag")))
    return items, False


async def _root_marker_exists(store: object, listing_prefix: str) -> bool:
    """Whether an object sits at the bare prefix key itself.

    obstore strips trailing slashes, so a folder marker *for* the prefix
    (``a/b/`` stored as ``a/b``) never appears in a listing under ``a/b/``.
    ``delete_prefix`` removes it with a HEAD-then-delete; ``ABSENT`` mirrors the
    HEAD so it means exactly "nothing ``delete_prefix`` would have removed".
    HEAD reads metadata only, never contents.
    """
    root = listing_prefix.rstrip("/")
    try:
        await obstore.head_async(store, root)  # type: ignore[arg-type]
    # conformance: ignore[E004] not-found is the expected answer; anything else re-raises
    except Exception as exc:
        if _is_not_found(exc) or _is_local_dir_collision(exc, store, root):  # type: ignore[arg-type]
            return False
        raise
    return True


async def _count_records(
    expectation: StoreRecords,
    store: object,
    listing_prefix: str,
    data: list[tuple[str, int, str | None]],
) -> dict[str, object]:
    """Read the check's files one at a time and count matching records.

    Returns:
        Observation fields: the counts and ``passed``, or ``problem`` when the
        files exceed a cap or one cannot be read or parsed.
    """
    suffixes = RECORD_FILE_SUFFIXES[expectation.format]
    files = [(key, size) for key, size, _ in data if key.endswith(suffixes)]
    if len(files) > MAX_RECORD_FILES:
        return {
            "problem": f"more than {MAX_RECORD_FILES} {expectation.format.value} "
            "files under the prefix; RECORDS cannot be graded"
        }
    if any(size > MAX_RECORD_FILE_BYTES for _, size in files):
        return {
            "problem": f"a file is larger than {MAX_RECORD_FILE_BYTES} bytes; "
            "RECORDS cannot be graded"
        }
    if sum(size for _, size in files) > MAX_RECORD_BYTES:
        return {
            "problem": f"the files total more than {MAX_RECORD_BYTES} bytes; "
            "RECORDS cannot be graded"
        }

    counter = (
        _store_records.count_parquet
        if expectation.format is RecordFormat.PARQUET
        else _store_records.count_jsonl
    )
    scanned = matched = 0
    for key, _ in files:
        try:
            result = await obstore.get_async(store, key)  # type: ignore[arg-type]
            payload = bytes(await result.bytes_async())
        # conformance: ignore[E004] the failure is the observation: reported in the verdict, which the harness grades as not passed
        except Exception as exc:
            logger.warning(
                "store-assert could not read a file (%s)",
                type(exc).__name__,
                exc_info=True,
            )
            return {"problem": f"reading a file failed: {type(exc).__name__}"}
        partitions = _store_records.partition_fields(key, listing_prefix)
        try:
            rows, hits = await run_in_thread(
                counter, payload, expectation.where, partitions
            )
        except _store_records.ParquetUnavailableError:
            return {
                "problem": "parquet checks need pyarrow, which this app does "
                "not install"
            }
        except _store_records.MalformedRecordsError as exc:
            return {
                "problem": f"a {expectation.format.value} file could not be "
                f"read as records: {exc}"
            }
        scanned += rows
        matched += hits
        del payload

    if expectation.count is not None:
        passed = matched == expectation.count
    else:
        passed = matched >= (expectation.at_least or 0)
    return {
        "files_scanned": len(files),
        "records_scanned": scanned,
        "records_matched": matched,
        "passed": passed,
    }


async def evaluate_expectation(
    expectation: StoreExpectation, store: object
) -> StoreObservation:
    """Evaluate one expectation against *store*. Never raises.

    Args:
        expectation: The claim.
        store: The obstore store to list.

    Returns:
        The observation; ``passed`` is False on any mismatch, malformed
        expectation, rejected prefix or failed listing.
    """
    observation = StoreObservation(
        prefix=expectation.prefix,
        kind=expectation.kind,
        expected_count=(
            expectation.count
            if isinstance(expectation, (StoreCount, StoreRecords))
            else None
        ),
        expected_at_least=(
            expectation.at_least if isinstance(expectation, StoreRecords) else None
        ),
    )
    try:
        listing_prefix = resolve_assert_prefix(expectation.prefix)
    except ValueError as exc:
        return observation.model_copy(update={"problem": str(exc)})

    # ABSENT needs only to know whether anything at all is there.
    limit = 1 if isinstance(expectation, StoreAbsent) else MAX_KEYS_SCANNED
    try:
        items, truncated = await _scan(store, listing_prefix, limit=limit)
    # conformance: ignore[E004] the failure is the observation: reported in the verdict, which the harness grades as not passed
    except Exception as exc:
        logger.warning(
            "store-assert listing failed for an expectation (%s)",
            type(exc).__name__,
            exc_info=True,
        )
        return observation.model_copy(
            update={"problem": f"listing failed: {type(exc).__name__}"}
        )

    if isinstance(expectation, StoreAbsent):
        found = len(items)
        if not found:
            try:
                found = int(await _root_marker_exists(store, listing_prefix))
            # conformance: ignore[E004] the failure is the observation: reported in the verdict, which the harness grades as not passed
            except Exception as exc:
                logger.warning(
                    "store-assert root-marker probe failed for an expectation (%s)",
                    type(exc).__name__,
                    exc_info=True,
                )
                return observation.model_copy(
                    update={
                        "problem": f"root-marker probe failed: {type(exc).__name__}"
                    }
                )
        return observation.model_copy(
            update={"objects_all": found, "passed": not found}
        )

    data = await run_in_thread(_drop_directory_markers, items)
    update: dict[str, object] = {
        "objects_all": len(items),
        "objects_data": len(data),
        "truncated": truncated,
    }
    if isinstance(expectation, StoreRecords):
        if truncated:
            update["problem"] = (
                f"more than {MAX_KEYS_SCANNED} objects under the prefix; "
                "RECORDS cannot be graded"
            )
            return observation.model_copy(update=update)
        update.update(await _count_records(expectation, store, listing_prefix, data))
        return observation.model_copy(update=update)
    if isinstance(expectation, StorePresent):
        update["passed"] = bool(data)
    else:
        update["passed"] = not truncated and len(data) == expectation.count
        if truncated:
            update["problem"] = (
                f"more than {MAX_KEYS_SCANNED} objects under the prefix; "
                "COUNT cannot be graded"
            )
    return observation.model_copy(update=update)


@activity.defn(name=STORE_ASSERT_ACTIVITY_NAME)
async def store_assert_activity(input: StoreAssertInput) -> StoreAssertOutput:
    """Evaluate every expectation against the worker's deployment store.

    Reads nothing unless this tenant opted in: the gate is checked before the
    store is even resolved.
    """
    if not constants.STORE_ASSERT_ENABLED:
        logger.warning(
            "store-assert refused %d expectation(s): ATLAN_STORE_ASSERT_ENABLED "
            "is not set on this deployment",
            len(input.expectations),
        )
        return StoreAssertOutput(enabled=False)
    store = _resolve_store(None)
    observations = [
        await evaluate_expectation(expectation, store)
        for expectation in input.expectations
    ]
    passed = bool(observations) and all(o.passed for o in observations)
    logger.info(
        "store-assert evaluated %d expectation(s): %d passed",
        len(observations),
        sum(o.passed for o in observations),
    )
    return StoreAssertOutput(enabled=True, passed=passed, observations=observations)


@workflow.defn(name=STORE_ASSERT_WORKFLOW_TYPE)
class StoreAssertWorkflow:
    """Durable wrapper around :func:`store_assert_activity`.

    Succeeds whatever the verdict: the verdict is the output, and the harness
    grades it. Failing the node instead would drop the structured output.
    """

    @workflow.run
    async def run(self, input: StoreAssertInput) -> StoreAssertOutput:
        return await workflow.execute_activity(
            STORE_ASSERT_ACTIVITY_NAME,
            input,
            result_type=StoreAssertOutput,
            start_to_close_timeout=_START_TO_CLOSE,
            retry_policy=_RETRY,
        )
