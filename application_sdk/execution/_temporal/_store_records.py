"""Record-level counting for ``sdk:store-assert`` ``RECORDS`` checks (FND-3571).

Pure functions over bytes already fetched from the store: no I/O, no Temporal.
Parquet and JSONL rows are both turned into plain Python values, so one
condition means the same thing in either format.

pyarrow is imported only inside :func:`count_parquet`. It is an optional SDK
extra, and importing this module (which every worker does) must not load it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Protocol
from urllib.parse import unquote

import orjson

#: Rows decoded per parquet batch; bounds memory to one batch of the projected
#: columns rather than a whole file.
PARQUET_BATCH_ROWS = 10_000


class ParquetUnavailableError(Exception):
    """pyarrow is not installed on this worker, so parquet cannot be read."""


class MalformedRecordsError(Exception):
    """A file's contents are not records in the declared format."""


class Condition(Protocol):
    """What :func:`record_matches` needs from a condition."""

    @property
    def path(self) -> tuple[str, ...]: ...

    def holds(self, value: object) -> bool: ...


class _Missing:
    """The field is not in the record at all (distinct from a null value)."""

    def __repr__(self) -> str:
        return "MISSING"


MISSING: Any = _Missing()


def partition_fields(key: str, listing_prefix: str) -> dict[str, str]:
    """Hive partition values from the directories between *listing_prefix* and
    the file: ``.../type_name=Table/part-0.parquet`` gives ``{"type_name":
    "Table"}``. Writers that partition this way leave the column out of the
    file, so a condition on it can only be answered from the path."""
    relative = key[len(listing_prefix) :] if key.startswith(listing_prefix) else key
    fields: dict[str, str] = {}
    for segment in relative.split("/")[:-1]:
        name, sep, value = segment.partition("=")
        if sep and name:
            fields[unquote(name)] = unquote(value)
    return fields


def lookup(
    record: Mapping[str, Any], path: Sequence[str], partitions: Mapping[str, str]
) -> object:
    """The value at dotted *path*, or :data:`MISSING`.

    The first segment is a column (or JSONL key); a column the file lacks falls
    back to a Hive partition value. Later segments walk nested objects — struct
    columns, JSON objects, and string columns holding JSON (publish stores
    whole entities that way), which are parsed on demand.
    """
    head = path[0]
    if head in record:
        value: object = record[head]
    elif head in partitions:
        value = partitions[head]
    else:
        return MISSING
    for segment in path[1:]:
        if isinstance(value, (str, bytes)):
            try:
                value = orjson.loads(value)
            except orjson.JSONDecodeError:
                return MISSING
        if not isinstance(value, Mapping) or segment not in value:
            return MISSING
        value = value[segment]
    return value


def has_value(value: object) -> bool:
    """Present, not null, and not empty (``""``, ``[]``, ``{}``)."""
    if value is MISSING or value is None:
        return False
    if isinstance(value, (str, bytes, list, tuple, dict)):
        return len(value) > 0
    return True


def equals(actual: object, expected: object) -> bool:
    """Type-aware equality: ``True`` never equals ``1``, and a string never
    equals a number, so a condition cannot pass by coercion."""
    if isinstance(expected, bool) or isinstance(actual, bool):
        return type(actual) is bool and type(expected) is bool and actual == expected
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return actual == expected
    if isinstance(expected, str) and isinstance(actual, str):
        return actual == expected
    return False


def record_matches(
    record: Mapping[str, Any],
    conditions: Iterable[Condition],
    partitions: Mapping[str, str],
) -> bool:
    """Whether every condition holds for *record*."""
    return all(c.holds(lookup(record, c.path, partitions)) for c in conditions)


def count_jsonl(
    data: bytes, conditions: Sequence[Condition], partitions: Mapping[str, str]
) -> tuple[int, int]:
    """Rows and matching rows in a JSON Lines file.

    Raises:
        MalformedRecordsError: A non-blank line is not a JSON object.
    """
    scanned = matched = 0
    for line in data.splitlines():
        if not line.strip():
            continue
        try:
            record = orjson.loads(line)
        except orjson.JSONDecodeError as exc:
            raise MalformedRecordsError("a line is not valid JSON") from exc
        if not isinstance(record, dict):
            raise MalformedRecordsError("a line is not a JSON object")
        scanned += 1
        matched += record_matches(record, conditions, partitions)
    return scanned, matched


def count_parquet(
    data: bytes, conditions: Sequence[Condition], partitions: Mapping[str, str]
) -> tuple[int, int]:
    """Rows and matching rows in a parquet file.

    Reads only the columns the conditions name; with no conditions, only the
    footer's row count.

    Raises:
        ParquetUnavailableError: pyarrow is not installed.
        MalformedRecordsError: *data* is not a parquet file.
    """
    try:
        import pyarrow as pa  # noqa: PLC0415 — optional extra; see module docstring
        import pyarrow.parquet as pq  # noqa: PLC0415
    except ImportError as exc:
        raise ParquetUnavailableError from exc
    try:
        parquet_file = pq.ParquetFile(pa.BufferReader(data))
    except (pa.ArrowInvalid, OSError) as exc:
        raise MalformedRecordsError("not a parquet file") from exc

    rows = parquet_file.metadata.num_rows
    if not conditions:
        return rows, rows
    columns = sorted(
        {c.path[0] for c in conditions} & set(parquet_file.schema_arrow.names)
    )
    if not columns:
        # Every condition is answered by partitions or MISSING: same for each row.
        return rows, rows if record_matches({}, conditions, partitions) else 0
    matched = 0
    for batch in parquet_file.iter_batches(
        batch_size=PARQUET_BATCH_ROWS, columns=columns
    ):
        matched += sum(
            record_matches(record, conditions, partitions)
            for record in batch.to_pylist()
        )
    return rows, matched
