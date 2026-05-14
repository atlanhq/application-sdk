"""Targeted unit tests for application_sdk.transformers.atlas.__init__.

These tests cover branches in AtlasTransformer that are not exercised by the
existing per-entity tests:

- transform_metadata: iterates a (mocked) daft.DataFrame, exercises the inline
  `import daft` (BLDX-1129 anchor), exercises the success / skipped / error
  branches around transform_row, and confirms daft.from_pylist is invoked.
- transform_row: unknown typename returns None and emits an error log.
- _enrich_entity_with_metadata: timestamp conversions, source_owner, comment
  fallback, source_id custom attribute.

No real threads, no asyncio, no HTTP, no real daft DataFrame, no file I/O. The
only side effect is in-memory mutation of stub objects.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from application_sdk.transformers.atlas import AtlasTransformer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeDataFrame:
    """A minimal stand-in for `daft.DataFrame` exposing iter_rows()."""

    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    def iter_rows(self):  # pragma: no cover - generator protocol trivial
        for row in self._rows:
            yield row


@pytest.fixture
def transformer() -> AtlasTransformer:
    return AtlasTransformer(connector_name="snowflake", tenant_id="default")


# ---------------------------------------------------------------------------
# __init__ wiring (BLDX-1129 inline import path)
# ---------------------------------------------------------------------------


def test_init_loads_entity_class_definitions():
    """Ensures the inline `from application_sdk.transformers.atlas.sql import …`
    in AtlasTransformer.__init__ executes (BLDX-1129 anchor)."""
    t = AtlasTransformer(
        connector_name="snowflake",
        tenant_id="t1",
        current_epoch="9999",
    )
    assert t.tenant_id == "t1"
    assert t.connector_name == "snowflake"
    assert t.current_epoch == "9999"
    expected_keys = {
        "DATABASE",
        "SCHEMA",
        "TABLE",
        "VIEW",
        "COLUMN",
        "MATERIALIZED VIEW",
        "FUNCTION",
        "TAG_REF",
        "PROCEDURE",
    }
    assert expected_keys.issubset(set(t.entity_class_definitions.keys()))


def test_init_default_current_epoch_is_zero():
    t = AtlasTransformer(connector_name="snowflake", tenant_id="default")
    assert t.current_epoch == "0"


# ---------------------------------------------------------------------------
# transform_row
# ---------------------------------------------------------------------------


def test_transform_row_unknown_typename_returns_none(transformer: AtlasTransformer):
    result = transformer.transform_row(
        "BOGUS_TYPE",
        {"x": 1},
        "wf",
        "run",
    )
    assert result is None


def test_transform_row_creator_failure_returns_none(transformer: AtlasTransformer):
    """If the entity creator raises, transform_row catches the exception and
    returns None (and logs the error). Uses a missing required field to force
    the creator to raise."""
    # DATABASE.get_attributes asserts database_name is not None.
    result = transformer.transform_row(
        "DATABASE",
        {},  # missing required database_name
        "wf",
        "run",
        connection_name="conn",
        connection_qualified_name="default/snowflake/1728518400",
    )
    assert result is None


def test_transform_row_lowercase_typename_is_normalized(
    transformer: AtlasTransformer,
):
    # Ensures `typename.upper()` converts "database" to a known key.
    # Enrichment pulls last-sync values from the SDK's execution +
    # correlation contexts (BLDX-1229).  Set them explicitly so the
    # assertions below don't depend on ambient process state.
    from application_sdk.observability import (
        CorrelationContext,
        ExecutionContext,
        set_correlation_context,
        set_execution_context,
    )

    set_execution_context(
        ExecutionContext(
            execution_type="workflow",
            workflow_id="dbt-AMBSvQPJ",
            workflow_run_id="3d4aa53e-f47a-4d2b-b120-79db652ff759",
        )
    )
    set_correlation_context(CorrelationContext(correlation_id="corr-xyz"))

    try:
        result = transformer.transform_row(
            "database",
            {
                "database_name": "DB",
            },
            "wf",
            "run",
            connection_name="conn",
            connection_qualified_name="default/snowflake/1728518400",
        )
    finally:
        set_execution_context(ExecutionContext())
        set_correlation_context(CorrelationContext())

    assert result is not None
    assert result["typeName"] == "Database"
    # Confirm enrichment metadata propagated and now reads from context,
    # not from the legacy ``workflow_id`` / ``workflow_run_id`` args.
    assert result["attributes"]["tenantId"] == "default"
    assert result["attributes"]["lastSyncWorkflowName"] == "dbt-AMBSvQPJ"
    assert result["attributes"]["lastSyncRun"] == "corr-xyz"


def test_transform_row_overrides_entity_class_definitions(
    transformer: AtlasTransformer,
):
    """Caller-supplied entity_class_definitions replace the default mapping."""
    fake_class = MagicMock()
    fake_class.get_attributes.side_effect = ValueError("boom")
    custom_defs = {"CUSTOM": fake_class}
    result = transformer.transform_row(
        "CUSTOM",
        {"x": 1},
        "wf",
        "run",
        entity_class_definitions=custom_defs,
    )
    assert result is None
    fake_class.get_attributes.assert_called_once()


def test_transform_row_uses_entity_class_to_construct_entity(
    transformer: AtlasTransformer,
):
    """Verifies the orchestration: get_attributes → enrich → construct entity →
    .dict() call. All side effects are in-memory on the mock."""
    fake_entity_instance = MagicMock()
    fake_entity_instance.dict.return_value = {
        "typeName": "Custom",
        "attributes": {"name": "x"},
    }
    fake_entity_class = MagicMock(return_value=fake_entity_instance)
    fake_entity_class.get_attributes = MagicMock(
        return_value={
            "attributes": {"name": "x"},
            "custom_attributes": {"k": "v"},
            "entity_class": fake_entity_class,
        }
    )
    custom_defs = {"CUSTOM": fake_entity_class}
    result = transformer.transform_row(
        "CUSTOM",
        {"foo": "bar"},
        "wf-1",
        "run-1",
        entity_class_definitions=custom_defs,
        connection_name="conn",
        connection_qualified_name="default/snowflake/1728518400",
    )
    assert result == {"typeName": "Custom", "attributes": {"name": "x"}}
    # Ensure the instance was built with merged attrs and ACTIVE status.
    args, kwargs = fake_entity_class.call_args
    assert "attributes" in kwargs
    assert "custom_attributes" in kwargs
    # Status is the EntityStatus.ACTIVE enum value (not None).
    assert kwargs["status"] is not None
    # The dict serialization is by_alias=True with empty/unset excluded.
    fake_entity_instance.dict.assert_called_once_with(
        by_alias=True, exclude_none=True, exclude_unset=True
    )


# ---------------------------------------------------------------------------
# transform_metadata (BLDX-1129 anchor: `import daft` is inline)
# ---------------------------------------------------------------------------


def test_transform_metadata_invokes_daft_from_pylist_with_transformed_rows(
    transformer: AtlasTransformer,
):
    """Patch `daft.from_pylist` to verify the inline `import daft` runs and the
    transformed row list is forwarded. This is the BLDX-1129 protection: if the
    inline import is removed or renamed, this test fails."""
    rows = [
        {"database_name": "DB1"},
        {"database_name": "DB2"},
    ]
    df = _FakeDataFrame(rows)

    sentinel = object()
    with patch("daft.from_pylist", return_value=sentinel) as mock_from_pylist:
        result = transformer.transform_metadata(
            "DATABASE",
            df,
            workflow_id="wf",
            workflow_run_id="run",
            connection={
                "connection_name": "conn",
                "connection_qualified_name": "default/snowflake/1728518400",
            },
        )

    assert result is sentinel
    mock_from_pylist.assert_called_once()
    [transformed_list] = mock_from_pylist.call_args[0]
    # Both rows successfully transformed
    assert len(transformed_list) == 2
    assert all(item["typeName"] == "Database" for item in transformed_list)


def test_transform_metadata_skips_rows_that_return_none(
    transformer: AtlasTransformer,
):
    """When transform_row returns None for a row, that row is skipped (logger
    warning) and not included in the resulting list."""
    rows = [
        {"database_name": "DB1"},
        {},  # invalid -> get_attributes raises -> transform_row returns None
    ]
    df = _FakeDataFrame(rows)

    with patch("daft.from_pylist") as mock_from_pylist:
        transformer.transform_metadata(
            "DATABASE",
            df,
            workflow_id="wf",
            workflow_run_id="run",
            connection={
                "connection_name": "conn",
                "connection_qualified_name": "default/snowflake/1728518400",
            },
        )

    [transformed_list] = mock_from_pylist.call_args[0]
    # Only the valid row survives
    assert len(transformed_list) == 1


def test_transform_metadata_handles_iter_rows_exception_per_row(
    transformer: AtlasTransformer,
):
    """If transform_row itself raises for one row, the exception is caught and
    iteration continues (logger.error)."""

    rows = [{"database_name": "DB_OK"}, {"database_name": "DB_BAD"}]
    df = _FakeDataFrame(rows)

    real_transform_row = transformer.transform_row
    call_count = {"n": 0}

    def flaky(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated downstream failure")
        return real_transform_row(*args, **kwargs)

    with patch.object(transformer, "transform_row", side_effect=flaky):
        with patch("daft.from_pylist") as mock_from_pylist:
            transformer.transform_metadata(
                "DATABASE",
                df,
                workflow_id="wf",
                workflow_run_id="run",
                connection={
                    "connection_name": "conn",
                    "connection_qualified_name": "default/snowflake/1728518400",
                },
            )

    [transformed_list] = mock_from_pylist.call_args[0]
    assert len(transformed_list) == 1


def test_transform_metadata_without_connection_kwarg_uses_none_connection(
    transformer: AtlasTransformer,
):
    """If no connection kwarg is provided, connection_name and
    connection_qualified_name fall through as None. No exception expected; the
    inline `import daft` still runs."""
    df = _FakeDataFrame([])

    with patch("daft.from_pylist") as mock_from_pylist:
        transformer.transform_metadata(
            "DATABASE",
            df,
            workflow_id="wf",
            workflow_run_id="run",
        )

    mock_from_pylist.assert_called_once()
    [rows] = mock_from_pylist.call_args[0]
    assert rows == []


def test_transform_metadata_uses_custom_entity_class_definitions(
    transformer: AtlasTransformer,
):
    """When entity_class_definitions is supplied, it replaces the default
    mapping for subsequent transform_row calls."""

    fake_entity_instance = MagicMock()
    fake_entity_instance.dict.return_value = {"typeName": "X"}
    fake_entity_class = MagicMock(return_value=fake_entity_instance)
    fake_entity_class.get_attributes = MagicMock(
        return_value={
            "attributes": {"name": "x"},
            "custom_attributes": {},
            "entity_class": fake_entity_class,
        }
    )
    custom_defs = {"CUSTOM": fake_entity_class}

    df = _FakeDataFrame([{"k": "v"}])
    with patch("daft.from_pylist") as mock_from_pylist:
        transformer.transform_metadata(
            "CUSTOM",
            df,
            workflow_id="wf",
            workflow_run_id="run",
            entity_class_definitions=custom_defs,
            connection={
                "connection_name": "conn",
                "connection_qualified_name": "default/snowflake/1728518400",
            },
        )

    [transformed_list] = mock_from_pylist.call_args[0]
    assert transformed_list == [{"typeName": "X"}]


# ---------------------------------------------------------------------------
# _enrich_entity_with_metadata
# ---------------------------------------------------------------------------


CONN_QN = "default/snowflake/1728518400"


def test_enrich_uses_remarks_first_for_description(transformer: AtlasTransformer):
    enriched = transformer._enrich_entity_with_metadata(
        "wf",
        "run",
        {
            "remarks": "<p>hello</p>",
            "comment": "ignored",
            "connection_qualified_name": CONN_QN,
        },
    )
    # process_text strips the HTML
    assert enriched["attributes"]["description"] == "hello"


def test_enrich_falls_back_to_comment_when_remarks_absent(
    transformer: AtlasTransformer,
):
    enriched = transformer._enrich_entity_with_metadata(
        "wf",
        "run",
        {"comment": "  hi  there  ", "connection_qualified_name": CONN_QN},
    )
    # process_text collapses whitespace
    assert enriched["attributes"]["description"] == "hi there"


def test_enrich_no_remarks_no_comment_no_description_key(
    transformer: AtlasTransformer,
):
    enriched = transformer._enrich_entity_with_metadata(
        "wf", "run", {"connection_qualified_name": CONN_QN}
    )
    assert "description" not in enriched["attributes"]


def test_enrich_propagates_source_owner_and_source_id(
    transformer: AtlasTransformer,
):
    enriched = transformer._enrich_entity_with_metadata(
        "wf",
        "run",
        {
            "source_owner": "alice",
            "source_id": "abc-123",
            "connection_qualified_name": CONN_QN,
        },
    )
    assert enriched["attributes"]["source_created_by"] == "alice"
    assert enriched["custom_attributes"]["source_id"] == "abc-123"


def test_enrich_converts_created_and_last_altered_timestamps(
    transformer: AtlasTransformer,
):
    enriched = transformer._enrich_entity_with_metadata(
        "wf",
        "run",
        {
            "created": 1_700_000_000_000,
            "last_altered": 1_700_000_500_000,
            "connection_qualified_name": CONN_QN,
        },
    )
    assert isinstance(enriched["attributes"]["source_created_at"], datetime)
    assert isinstance(enriched["attributes"]["source_updated_at"], datetime)


def test_enrich_sets_workflow_metadata(transformer: AtlasTransformer):
    enriched = transformer._enrich_entity_with_metadata(
        "wf-id",
        "run-id",
        {"connection_name": "myconn", "connection_qualified_name": CONN_QN},
    )
    attrs = enriched["attributes"]
    assert attrs["last_sync_workflow_name"] == "wf-id"
    assert attrs["last_sync_run"] == "run-id"
    assert attrs["connection_name"] == "myconn"
    assert attrs["tenant_id"] == "default"
    # last_sync_run_at should be epoch ms (int, > 0)
    assert isinstance(attrs["last_sync_run_at"], int)
    assert attrs["last_sync_run_at"] > 0
