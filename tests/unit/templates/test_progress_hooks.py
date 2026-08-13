"""Framework ``mark_progress()`` hooks on the record emission path (FND-288).

Covers the two shapes in the SQL templates (ADR-0018 → *Feeding the tracker*):

* ``SqlApp._extract_entity`` — fetch a page, write a batch, repeat. Marking at
  the page boundary is what makes the read loop need no hold: only a *single*
  fetch+write cycle slower than ``max_no_progress_seconds`` needs one.
* ``SqlMetadataExtractor.fetch_*`` — page loops with no write side at all, so
  nothing else would carry the signal for them.

Also pins the deliberate *absence* of a hook in ``_transform_entity``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import AsyncMock, patch

import pytest

from application_sdk.templates.contracts.sql_metadata import (
    ExtractionTaskInput,
    FetchColumnsInput,
    FetchDatabasesInput,
    FetchSchemasInput,
    FetchTablesInput,
    TransformInput,
)
from application_sdk.templates.sql_app import SqlApp
from application_sdk.templates.sql_metadata_extractor import SqlMetadataExtractor
from tests.unit.conftest import RecordingProgressTracker


class _PagedSQLClient:
    """Yields a fixed list of pages from ``run_query``, one page per iteration."""

    def __init__(self, pages: list[list[dict[str, Any]]] | None = None) -> None:
        self._pages = pages or []
        self.closed = False
        self.last_query: str | None = None

    async def load(self, credentials: Any = None) -> None:
        pass

    async def run_query(self, query: str, batch_size: int = 100000):
        self.last_query = query
        for page in self._pages:
            yield page

    async def close(self) -> None:
        self.closed = True


def _task_input(output_path: str, **kwargs: Any) -> TransformInput:
    defaults: dict[str, Any] = {
        "workflow_id": "test-wf",
        "output_path": output_path,
        "output_prefix": "/tmp",
        "exclude_filter": "",
        "include_filter": "",
        "temp_table_regex": "",
    }
    defaults.update(kwargs)
    return TransformInput(**defaults)


class _App(SqlApp):
    sql_client_class: ClassVar = _PagedSQLClient  # type: ignore[assignment]
    _app_registered: ClassVar[bool] = True
    fetch_database_sql: ClassVar[str] = "SELECT database_name FROM databases"

    def map_database(self, record: dict[str, Any], connection_qn: str) -> dict:
        return {"typeName": "Database", "qualifiedName": connection_qn}


# ---------------------------------------------------------------------------
# extract_* — page boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_marks_once_per_page_never_per_record(
    tmp_path: Path, progress_marks: RecordingProgressTracker
) -> None:
    pages = [[{"database_name": f"db{i}-{j}"} for j in range(50)] for i in range(4)]
    client = _PagedSQLClient(pages=pages)
    app = _App()

    with patch.object(app, "_init_sql_client", AsyncMock(return_value=client)):
        result = await app.extract_databases(_task_input(str(tmp_path)))

    assert result.total_record_count == 200
    assert progress_marks.count("extract.page") == 4
    # The constraint made explicit: 200 records, 4 marks.
    assert len(progress_marks.labels) == 4


@pytest.mark.asyncio
async def test_an_extract_that_yields_no_pages_marks_nothing(
    tmp_path: Path, progress_marks: RecordingProgressTracker
) -> None:
    client = _PagedSQLClient(pages=[])
    app = _App()

    with patch.object(app, "_init_sql_client", AsyncMock(return_value=client)):
        await app.extract_databases(_task_input(str(tmp_path)))

    assert progress_marks.labels == []


@pytest.mark.asyncio
async def test_transform_deliberately_records_no_progress(
    tmp_path: Path, progress_marks: RecordingProgressTracker
) -> None:
    """``_transform_entity`` is covered by a hold, not by a framework hook.

    FND-282 offloaded the whole record map to a single ``run_in_thread`` hop, so
    it is FND-290's unbounded auto-hold that vouches for it — mechanism 2, not
    mechanism 1. There is also no boundary to hook: the loop is line-by-line,
    and marking per record is the one thing ADR-0018 forbids outright.

    Pinned as a test because the tempting change here is wrong in both
    directions — adding a per-record mark violates the hard constraint, and
    adding one *outside* the offloaded callable would mark once for a transform
    that can run for hours.
    """
    raw_dir = tmp_path / "raw" / "database"
    raw_dir.mkdir(parents=True)
    (raw_dir / "records.json").write_text(
        "\n".join(json.dumps({"database_name": f"db{i}"}) for i in range(20)) + "\n"
    )
    app = _App()

    result = await app.transform_databases(_task_input(str(tmp_path)))

    assert result.total_record_count == 20
    assert progress_marks.labels == []


# ---------------------------------------------------------------------------
# fetch_* — page loops with no write side
# ---------------------------------------------------------------------------


def _extractor(client: _PagedSQLClient, **attrs: str) -> SqlMetadataExtractor:
    class _E(SqlMetadataExtractor):
        _app_registered = True
        sql_client_class = _PagedSQLClient  # type: ignore[assignment]

    for name, value in attrs.items():
        setattr(_E, name, value)

    extractor = _E.__new__(_E)

    async def _fake_load(_input: ExtractionTaskInput) -> _PagedSQLClient:
        return client

    extractor._load_sql_client = _fake_load  # type: ignore[method-assign]
    return extractor


@pytest.mark.parametrize(
    ("attr", "sql", "row_key", "method", "input_cls", "label"),
    [
        (
            "fetch_database_sql",
            "SELECT db FROM meta",
            "database_name",
            "fetch_databases",
            FetchDatabasesInput,
            "fetch_databases.page",
        ),
        (
            "fetch_schema_sql",
            "SELECT s FROM meta",
            "schema_name",
            "fetch_schemas",
            FetchSchemasInput,
            "fetch_schemas.page",
        ),
        (
            "fetch_table_sql",
            "SELECT t FROM meta",
            "table_name",
            "fetch_tables",
            FetchTablesInput,
            "fetch_tables.page",
        ),
        (
            "fetch_column_sql",
            "SELECT c FROM meta",
            "column_name",
            "fetch_columns",
            FetchColumnsInput,
            "fetch_columns.page",
        ),
    ],
)
@pytest.mark.asyncio
async def test_each_fetch_task_marks_once_per_page(
    attr: str,
    sql: str,
    row_key: str,
    method: str,
    input_cls: type,
    label: str,
    progress_marks: RecordingProgressTracker,
) -> None:
    pages = [[{row_key: f"v{i}-{j}"} for j in range(10)] for i in range(3)]
    client = _PagedSQLClient(pages=pages)
    extractor = _extractor(client, **{attr: sql})

    await getattr(extractor, method)(input_cls())

    assert progress_marks.count(label) == 3
    assert len(progress_marks.labels) == 3


@pytest.mark.asyncio
async def test_fetch_tasks_are_inert_without_a_bound_tracker() -> None:
    """No ``progress_marks`` fixture here: no tracker is bound, on purpose.

    Outside an activity ``current_progress_tracker()`` hands back the inert
    tracker, so every hook discards its signal and behaviour is unchanged.
    """
    pages = [[{"database_name": "db1"}]]
    client = _PagedSQLClient(pages=pages)
    extractor = _extractor(client, fetch_database_sql="SELECT db FROM meta")

    out = await extractor.fetch_databases(FetchDatabasesInput())

    assert out.databases == ["db1"]
    assert client.closed is True
