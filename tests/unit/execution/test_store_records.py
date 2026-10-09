"""``RECORDS`` checks of the object-store assertion workflow (FND-3571)."""

from __future__ import annotations

import builtins
import io
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

import obstore
import orjson
import pyarrow as pa
import pyarrow.parquet as pq
import pydantic
import pytest
from obstore.store import MemoryStore

from application_sdk.execution._temporal import store_assert
from application_sdk.execution._temporal.store_assert import (
    FieldCondition,
    FieldOp,
    RecordFormat,
    StoreAssertInput,
    StoreRecords,
    evaluate_expectation,
)

ROOT = "persistent-artifacts/publish/default/postgres/run-1"

# Shaped like publish's state records: flat columns, an `entity` column holding
# the whole entity as a JSON string, and a struct column.
ROWS: list[dict[str, Any]] = [
    {
        "type_name": "Table",
        "qualified_name": "default/postgres/run-1/db/sch/t1",
        "state": "SYNCED",
        "is_duplicate": False,
        "column_count": 3,
        "error_code": None,
        "entity": orjson.dumps(
            {"attributes": {"qualifiedName": "q1", "name": "t1"}}
        ).decode(),
        "ars": {"name": "t1", "attributes": {"connectorName": "postgres"}},
    },
    {
        "type_name": "Table",
        "qualified_name": "",
        "state": "FAILED",
        "is_duplicate": True,
        "column_count": 1,
        "error_code": "ATLAS-400",
        "entity": orjson.dumps({"attributes": {"name": "t2"}}).decode(),
        "ars": {"name": "t2", "attributes": {"connectorName": "postgres"}},
    },
    {
        "type_name": "Column",
        "qualified_name": "default/postgres/run-1/db/sch/t1/c1",
        "state": "SYNCED",
        "is_duplicate": False,
        "column_count": 0,
        "error_code": None,
        "entity": orjson.dumps(
            {"attributes": {"qualifiedName": "q3", "name": "c1"}}
        ).decode(),
        "ars": {"name": "c1", "attributes": {"connectorName": "postgres"}},
    },
]


def _parquet(rows: list[dict[str, Any]]) -> bytes:
    buffer = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(rows), buffer)
    return buffer.getvalue()


def _jsonl(rows: list[dict[str, Any]]) -> bytes:
    return b"\n".join(orjson.dumps(r) for r in rows) + b"\n"


_ENCODE = {RecordFormat.PARQUET: _parquet, RecordFormat.JSONL: _jsonl}
_SUFFIX = {RecordFormat.PARQUET: ".parquet", RecordFormat.JSONL: ".json"}


def _store_with(fmt: RecordFormat, rows: list[dict[str, Any]] = ROWS) -> MemoryStore:
    store = MemoryStore()
    obstore.put(store, f"{ROOT}/part-0{_SUFFIX[fmt]}", _ENCODE[fmt](rows))
    return store


def _cond(field: str, op: FieldOp, **kw: Any) -> FieldCondition:
    return FieldCondition(field=field, op=op, **kw)


def _records(fmt: RecordFormat, *where: FieldCondition, **bound: int) -> StoreRecords:
    return StoreRecords(prefix=ROOT, format=fmt, where=list(where), **bound)


async def _matched(store: MemoryStore, check: StoreRecords) -> int | None:
    observation = await evaluate_expectation(check, store)
    assert not observation.problem, observation.problem
    return observation.records_matched


FORMATS = [
    pytest.param(RecordFormat.PARQUET, id="parquet"),
    pytest.param(RecordFormat.JSONL, id="jsonl"),
]


# ---------------------------------------------------------------------------
# One condition means the same thing in both formats
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", FORMATS)
@pytest.mark.parametrize(
    ("where", "expected"),
    [
        pytest.param((), 3, id="all-rows"),
        pytest.param((_cond("type_name", FieldOp.EQ, value="Table"),), 2, id="eq"),
        pytest.param(
            (_cond("state", FieldOp.IN, values=["FAILED", "DELETED"]),), 1, id="in"
        ),
        pytest.param(
            (
                _cond("type_name", FieldOp.EQ, value="Table"),
                _cond("state", FieldOp.EQ, value="SYNCED"),
            ),
            1,
            id="conditions-and",
        ),
        pytest.param(
            (_cond("qualified_name", FieldOp.PRESENT),), 2, id="present-skips-empty"
        ),
        pytest.param((_cond("error_code", FieldOp.MISSING),), 2, id="missing-is-null"),
        pytest.param((_cond("no_such_field", FieldOp.MISSING),), 3, id="absent-col"),
        pytest.param((_cond("column_count", FieldOp.EQ, value=3),), 1, id="int"),
        pytest.param((_cond("is_duplicate", FieldOp.EQ, value=True),), 1, id="bool"),
        pytest.param(
            (_cond("is_duplicate", FieldOp.EQ, value=1),), 0, id="bool-is-not-int"
        ),
        pytest.param(
            (_cond("column_count", FieldOp.EQ, value="3"),), 0, id="str-is-not-int"
        ),
        pytest.param(
            (_cond("entity.attributes.qualifiedName", FieldOp.PRESENT),),
            2,
            id="json-string-column",
        ),
        pytest.param(
            (_cond("ars.attributes.connectorName", FieldOp.EQ, value="postgres"),),
            3,
            id="struct-path",
        ),
    ],
)
async def test_conditions_agree_across_formats(
    fmt: RecordFormat, where: tuple[FieldCondition, ...], expected: int
) -> None:
    store = _store_with(fmt)
    observation = await evaluate_expectation(
        _records(fmt, *where, count=expected), store
    )
    assert observation.problem == ""
    assert (observation.records_scanned, observation.records_matched) == (3, expected)
    assert observation.files_scanned == 1
    assert observation.passed


@pytest.mark.parametrize("fmt", FORMATS)
async def test_hive_partition_directories_answer_missing_columns(
    fmt: RecordFormat,
) -> None:
    """publish's staging caches write `.../type_name=Table/part.parquet` and
    leave type_name out of the file."""
    store = MemoryStore()
    for type_name, rows in (("Table", ROWS[:2]), ("Column", ROWS[2:])):
        stripped = [{k: v for k, v in r.items() if k != "type_name"} for r in rows]
        obstore.put(
            store,
            f"{ROOT}/partition_id=0/type_name={type_name}/part-0{_SUFFIX[fmt]}",
            _ENCODE[fmt](stripped),
        )
    check = _records(fmt, _cond("type_name", FieldOp.EQ, value="Table"), count=2)
    assert await _matched(store, check) == 2
    pid = _records(fmt, _cond("partition_id", FieldOp.EQ, value="0"), count=3)
    assert await _matched(store, pid) == 3


async def test_a_column_in_the_file_wins_over_a_partition_directory() -> None:
    store = MemoryStore()
    obstore.put(store, f"{ROOT}/type_name=Column/part-0.json", _jsonl(ROWS[:1]))
    check = _records(
        RecordFormat.JSONL, _cond("type_name", FieldOp.EQ, value="Table"), count=1
    )
    assert await _matched(store, check) == 1


async def test_counts_sum_across_files_and_ignore_other_formats() -> None:
    store = MemoryStore()
    obstore.put(store, f"{ROOT}/a.parquet", _parquet(ROWS))
    obstore.put(store, f"{ROOT}/sub/b.parquet", _parquet(ROWS[:1]))
    obstore.put(store, f"{ROOT}/a.parquet.sha256", b"deadbeef")
    obstore.put(store, f"{ROOT}/c.json", _jsonl(ROWS))
    observation = await evaluate_expectation(
        _records(RecordFormat.PARQUET, count=4), store
    )
    assert (observation.files_scanned, observation.records_scanned) == (2, 4)
    assert observation.passed


async def test_at_least_and_a_miss() -> None:
    store = _store_with(RecordFormat.PARQUET)
    assert (
        await evaluate_expectation(_records(RecordFormat.PARQUET, at_least=2), store)
    ).passed
    miss = await evaluate_expectation(_records(RecordFormat.PARQUET, count=4), store)
    assert not miss.passed
    assert (miss.records_matched, miss.problem) == (3, "")


async def test_no_files_counts_zero() -> None:
    observation = await evaluate_expectation(
        _records(RecordFormat.JSONL, count=0), MemoryStore()
    )
    assert observation.passed
    assert observation.files_scanned == 0


# ---------------------------------------------------------------------------
# Failure modes: each fails the check, none passes it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fmt", "content", "problem"),
    [
        pytest.param(RecordFormat.JSONL, b'{"a": 1}\nnot json\n', "not valid JSON"),
        pytest.param(RecordFormat.JSONL, b"[1, 2]\n", "not a JSON object"),
        pytest.param(RecordFormat.PARQUET, b"not parquet", "not a parquet file"),
    ],
)
async def test_unreadable_content_is_a_problem(
    fmt: RecordFormat, content: bytes, problem: str
) -> None:
    store = MemoryStore()
    obstore.put(store, f"{ROOT}/x{_SUFFIX[fmt]}", content)
    observation = await evaluate_expectation(_records(fmt, count=0), store)
    assert not observation.passed
    assert problem in observation.problem


def _parquet_with_corrupt_pages() -> bytes:
    """Footer intact, every data page overwritten: opens, then fails to decode."""
    data = bytearray(_parquet([{"type_name": f"T{i % 7}" * 20} for i in range(5000)]))
    footer_len = int.from_bytes(data[-8:-4], "little")
    body_end = len(data) - 8 - footer_len
    data[8 : body_end - 8] = b"\xff" * (body_end - 16)
    return bytes(data)


async def test_a_corrupt_parquet_page_is_a_problem_not_a_raise() -> None:
    store = MemoryStore()
    obstore.put(store, f"{ROOT}/x.parquet", _parquet_with_corrupt_pages())
    check = _records(
        RecordFormat.PARQUET, _cond("type_name", FieldOp.EQ, value="T0"), count=0
    )
    observation = await evaluate_expectation(check, store)
    assert not observation.passed
    assert "a data page could not be decoded" in observation.problem


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        pytest.param(b'{"a": 1}\r\n{"a": 2}\r\n', 2, id="crlf"),
        pytest.param(b'{"a": 1}\n{"a": 2}', 2, id="no-trailing-newline"),
        pytest.param(b'\n\n{"a": 1}\n  \n', 1, id="blank-lines"),
        pytest.param(b"", 0, id="empty"),
    ],
)
async def test_jsonl_line_endings(content: bytes, expected: int) -> None:
    store = MemoryStore()
    obstore.put(store, f"{ROOT}/x.jsonl", content)
    observation = await evaluate_expectation(
        _records(RecordFormat.JSONL, count=expected), store
    )
    assert observation.problem == ""
    assert observation.records_scanned == expected


@pytest.mark.parametrize(
    ("cap", "value"),
    [
        pytest.param("MAX_RECORD_FILE_BYTES", 100, id="file-bytes"),
        pytest.param("MAX_RECORD_BYTES", 100, id="total-bytes"),
    ],
)
async def test_an_object_that_grew_after_listing_is_not_read(
    monkeypatch: pytest.MonkeyPatch, cap: str, value: int
) -> None:
    """The listing said small; the GET's own size is what decides the read."""
    store = MemoryStore()
    obstore.put(store, f"{ROOT}/a.json", b"{}\n")
    monkeypatch.setattr(store_assert, cap, value)

    class _Grown:
        meta = {"size": value + 1}

        async def bytes_async(self) -> bytes:
            raise AssertionError("read a body past a cap")

    async def _get(*a: Any, **k: Any) -> _Grown:
        return _Grown()

    monkeypatch.setattr(
        store_assert, "obstore", SimpleNamespace(list=obstore.list, get_async=_get)
    )
    observation = await evaluate_expectation(
        _records(RecordFormat.JSONL, count=1), store
    )
    assert not observation.passed
    assert "grew past the byte caps" in observation.problem


@pytest.mark.parametrize(
    ("cap", "value"),
    [
        pytest.param("MAX_RECORD_FILES", 1, id="files"),
        pytest.param("MAX_RECORD_FILE_BYTES", 10, id="file-bytes"),
        pytest.param("MAX_RECORD_BYTES", 10, id="total-bytes"),
    ],
)
async def test_caps_make_the_check_ungradable_before_reading(
    monkeypatch: pytest.MonkeyPatch, cap: str, value: int
) -> None:
    store = MemoryStore()
    obstore.put(store, f"{ROOT}/a.json", _jsonl(ROWS))
    obstore.put(store, f"{ROOT}/b.json", _jsonl(ROWS))
    monkeypatch.setattr(store_assert, cap, value)

    async def _must_not_get(*a: Any, **k: Any) -> Any:
        raise AssertionError("read a file past a cap")

    monkeypatch.setattr(
        store_assert,
        "obstore",
        SimpleNamespace(list=obstore.list, get_async=_must_not_get),
    )
    observation = await evaluate_expectation(
        _records(RecordFormat.JSONL, count=6), store
    )
    assert not observation.passed
    assert "cannot be graded" in observation.problem


async def test_without_pyarrow_a_parquet_check_fails_clearly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store_with(RecordFormat.PARQUET)
    real_import = builtins.__import__

    def _no_pyarrow(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "pyarrow" or name.startswith("pyarrow."):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_pyarrow)
    observation = await evaluate_expectation(
        _records(RecordFormat.PARQUET, count=3), store
    )
    assert not observation.passed
    assert observation.problem == (
        "parquet checks need pyarrow, which this app does not install"
    )


async def test_a_failed_get_reports_the_type_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store_with(RecordFormat.JSONL)

    async def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("s3://internal-bucket/secret-detail")

    monkeypatch.setattr(
        store_assert, "obstore", SimpleNamespace(list=obstore.list, get_async=_boom)
    )
    observation = await evaluate_expectation(
        _records(RecordFormat.JSONL, count=3), store
    )
    assert observation.problem == "reading a file failed: RuntimeError"


async def test_records_only_list_and_get(monkeypatch: pytest.MonkeyPatch) -> None:
    """No put or delete exists on the module's view of obstore."""
    store = _store_with(RecordFormat.PARQUET)
    monkeypatch.setattr(
        store_assert,
        "obstore",
        SimpleNamespace(list=obstore.list, get_async=obstore.get_async),
    )
    check = _records(
        RecordFormat.PARQUET, _cond("type_name", FieldOp.EQ, value="Table"), count=2
    )
    assert (await evaluate_expectation(check, store)).passed


def test_importing_the_workflow_does_not_load_pyarrow() -> None:
    """pyarrow is an optional extra; every worker imports store_assert."""
    code = (
        "import sys, application_sdk.execution._temporal.store_assert; "
        "print('pyarrow' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "False"


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param({"field": "a", "op": "eq"}, id="eq-without-value"),
        pytest.param(
            {"field": "a", "op": "present", "value": 1}, id="value-on-present"
        ),
        pytest.param({"field": "a", "op": "in", "values": []}, id="in-empty"),
        pytest.param({"field": "a", "op": "in", "value": "x"}, id="in-with-value"),
        pytest.param({"field": "a..b", "op": "present"}, id="empty-segment"),
        pytest.param({"field": "a", "op": "eq", "value": None}, id="eq-null"),
        pytest.param({"field": "a", "op": "eq", "value": "x" * 1025}, id="long-value"),
        pytest.param(
            {"field": "a", "op": "in", "values": list(range(21))}, id="too-many-values"
        ),
    ],
)
def test_malformed_conditions_are_rejected(raw: dict[str, Any]) -> None:
    with pytest.raises(pydantic.ValidationError):
        FieldCondition.model_validate(raw)


@pytest.mark.parametrize(
    "bound",
    [
        pytest.param({}, id="neither"),
        pytest.param({"count": 1, "at_least": 1}, id="both"),
    ],
)
def test_records_needs_exactly_one_bound(bound: dict[str, int]) -> None:
    with pytest.raises(pydantic.ValidationError):
        StoreRecords(prefix=ROOT, format=RecordFormat.JSONL, **bound)


def test_too_many_conditions_are_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        _records(
            RecordFormat.JSONL,
            *[_cond("a", FieldOp.PRESENT)] * 11,
            count=0,
        )


def test_records_round_trips_through_json_keeping_value_types() -> None:
    sent = StoreAssertInput(
        expectations=[
            _records(
                RecordFormat.PARQUET,
                _cond("is_duplicate", FieldOp.EQ, value=True),
                _cond("column_count", FieldOp.IN, values=[1, 2.5, "3"]),
                at_least=1,
            )
        ]
    )
    received = StoreAssertInput.model_validate(
        sent.model_dump(mode="json", include={"expectations"})
    )
    assert received.expectations == sent.expectations
    where = received.expectations[0].where  # type: ignore[union-attr]
    assert type(where[0].value) is bool
    assert [type(v) for v in where[1].values or ()] == [int, float, str]
