"""Targeted unit tests for application_sdk.transformers.atlas.sql.

These tests target previously uncovered branches in:
- TagAttachment.get_attributes (object_domain handling, classification_defs).
- Function.get_attributes (Tabular vs Scalar, flag toggles, missing fields).
- Column.get_attributes (DYNAMIC TABLE, partition columns, unknown table_type).
- Table.get_attributes (DYNAMIC TABLE via is_dynamic, parent_table_partition,
  Materialized-View-only fields, custom attributes).
- AssertionError paths in Procedure / Database / Schema / Table / Column / Function.
- Function.Attributes.create / TagAttachment.Attributes.create exercised through
  their respective creator() classmethods (no real network or I/O).

All side effects (pyatlan model mutations) are constrained to the in-memory
class methods. No threads, no asyncio, no HTTP, no file I/O.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from application_sdk.transformers.atlas.sql import (
    Column,
    Database,
    Function,
    Procedure,
    Schema,
    Table,
    TagAttachment,
)

CONN = "default/snowflake/1728518400"


# ---------------------------------------------------------------------------
# Procedure
# ---------------------------------------------------------------------------


def test_procedure_missing_field_raises_value_error():
    with pytest.raises(ValueError, match="Procedure name"):
        Procedure.get_attributes({})


def test_procedure_uses_default_sub_type_when_procedure_type_absent():
    obj = {
        "procedure_name": "DO_X",
        "procedure_definition": "BEGIN END;",
        "procedure_catalog": "DB",
        "procedure_schema": "SCH",
        "connection_qualified_name": CONN,
    }
    result = Procedure.get_attributes(obj)
    assert result["attributes"]["sub_type"] == "-1"
    assert result["attributes"]["qualified_name"] == f"{CONN}/DB/SCH/_procedures_/DO_X"
    assert result["attributes"]["atlanSchema"]["typeName"] == "Schema"
    assert result["entity_class"] is Procedure


# ---------------------------------------------------------------------------
# Database / Schema assertion paths
# ---------------------------------------------------------------------------


def test_database_non_string_name_raises():
    with pytest.raises(ValueError, match="Database name"):
        Database.get_attributes(
            {"database_name": 123, "connection_qualified_name": CONN}
        )


def test_database_with_catalog_id_populates_custom_attribute():
    result = Database.get_attributes(
        {
            "database_name": "DB",
            "connection_qualified_name": CONN,
            "catalog_id": "abc-123",
            "schema_count": 5,
        }
    )
    assert result["custom_attributes"]["catalog_id"] == "abc-123"
    assert result["attributes"]["schema_count"] == 5


def test_schema_managed_access_and_catalog_id_populated():
    result = Schema.get_attributes(
        {
            "schema_name": "S",
            "catalog_name": "DB",
            "connection_qualified_name": CONN,
            "catalog_id": "cat-1",
            "is_managed_access": True,
        }
    )
    assert result["custom_attributes"]["catalog_id"] == "cat-1"
    assert result["custom_attributes"]["is_managed_access"] is True
    assert result["attributes"]["database"]["typeName"] == "Database"


def test_schema_missing_required_raises():
    with pytest.raises(ValueError, match="Schema name"):
        Schema.get_attributes({"connection_qualified_name": CONN})


# ---------------------------------------------------------------------------
# Table branches
# ---------------------------------------------------------------------------


def _table_base() -> Dict[str, Any]:
    return {
        "table_name": "T",
        "table_schema": "S",
        "table_catalog": "DB",
        "connection_qualified_name": CONN,
    }


def test_table_dynamic_via_is_dynamic_yes_returns_snowflake_dynamic_table():
    from pyatlan.model import assets

    obj = _table_base()
    obj["table_type"] = "WHATEVER"
    obj["is_dynamic"] = "YES"
    result = Table.get_attributes(obj)
    assert result["entity_class"] is assets.SnowflakeDynamicTable


def test_table_materialized_view_keeps_view_definition_and_mv_only_fields():
    from pyatlan.model import assets

    obj = _table_base()
    obj.update(
        {
            "table_type": "MATERIALIZED VIEW",
            "view_definition": "SELECT 1",
            "refresh_mode": "FULL",
            "staleness": "FRESH",
            "stale_since_date": "2024-01-01",
            "refresh_method": "FULL",
        }
    )
    result = Table.get_attributes(obj)
    assert result["entity_class"] is assets.MaterialisedView
    attrs = result["attributes"]
    assert attrs["definition"] == "SELECT 1"
    assert attrs["refresh_mode"] == "FULL"
    assert attrs["staleness"] == "FRESH"
    assert attrs["stale_since_date"] == "2024-01-01"
    assert attrs["refresh_method"] == "FULL"


def test_table_view_fallback_for_unknown_type():
    from pyatlan.model import assets

    obj = _table_base()
    obj["table_type"] = "WEIRD_VIEW"
    result = Table.get_attributes(obj)
    assert result["entity_class"] is assets.View


def test_table_partition_with_partitioned_parent_uses_parent_table_partition():
    obj = _table_base()
    obj.update(
        {
            "is_partition": True,
            "parent_table_name": "PARENT",
            "partitioned_parent_table": "PARENT",
        }
    )
    result = Table.get_attributes(obj)
    attrs = result["attributes"]
    assert "parent_table_partition" in attrs
    assert "parent_table" not in attrs
    assert attrs["table_qualified_name"] == f"{CONN}/DB/S/PARENT"


def test_table_collects_optional_custom_attributes():
    obj = _table_base()
    obj.update(
        {
            "is_transient": "YES",
            "table_catalog_id": "c-1",
            "table_schema_id": "s-1",
            "last_ddl": "2024-01-01",
            "last_ddl_by": "alice",
            "is_secure": "YES",
            "retention_time": "30",
            "stage_url": "s3://bucket",
            "is_insertable_into": "YES",
            "number_columns_in_part_key": "1",
            "columns_participating_in_part_key": "id",
            "is_typed": "NO",
            "auto_clustering_on": "ON",
            "engine": "InnoDB",
            "auto_increment": "5",
        }
    )
    result = Table.get_attributes(obj)
    custom = result["custom_attributes"]
    assert custom["is_transient"] == "YES"
    assert custom["catalog_id"] == "c-1"
    assert custom["schema_id"] == "s-1"
    assert custom["last_ddl"] == "2024-01-01"
    assert custom["last_ddl_by"] == "alice"
    assert custom["is_secure"] == "YES"
    assert custom["retention_time"] == "30"
    assert custom["stage_url"] == "s3://bucket"
    assert custom["is_insertable_into"] == "YES"
    assert custom["number_columns_in_part_key"] == "1"
    assert custom["columns_participating_in_part_key"] == "id"
    assert custom["is_typed"] == "NO"
    assert custom["auto_clustering_on"] == "ON"
    assert custom["engine"] == "InnoDB"
    assert custom["auto_increment"] == "5"


def test_table_missing_required_field_raises():
    with pytest.raises(ValueError, match="Table name"):
        Table.get_attributes({})


# ---------------------------------------------------------------------------
# Column branches
# ---------------------------------------------------------------------------


def _column_base() -> Dict[str, Any]:
    return {
        "column_name": "C",
        "table_catalog": "DB",
        "table_schema": "S",
        "table_name": "T",
        "ordinal_position": 1,
        "data_type": "VARCHAR",
        "connection_qualified_name": CONN,
    }


def test_column_missing_required_raises():
    with pytest.raises(ValueError, match="Column name"):
        Column.get_attributes({})


def test_column_view_table_type_uses_view_relationship():
    from pyatlan.model import assets

    obj = _column_base()
    obj["table_type"] = "VIEW"
    result = Column.get_attributes(obj)
    attrs = result["attributes"]
    assert attrs["parent_type"] is assets.View
    assert attrs["view"]["typeName"] == "View"
    assert attrs["view_name"] == "T"


def test_column_materialized_view_table_type():
    from pyatlan.model import assets

    obj = _column_base()
    obj["table_type"] = "MATERIALIZED VIEW"
    result = Column.get_attributes(obj)
    assert result["attributes"]["parent_type"] is assets.MaterialisedView
    assert result["attributes"]["materialisedView"]["typeName"] == "MaterialisedView"


def test_column_dynamic_table_via_is_dynamic_flag():
    from pyatlan.model import assets

    obj = _column_base()
    obj["is_dynamic"] = "YES"
    result = Column.get_attributes(obj)
    assert result["attributes"]["parent_type"] is assets.SnowflakeDynamicTable
    assert result["attributes"]["dynamicTable"]["typeName"] == "SnowflakeDynamicTable"


def test_column_partition_column_uses_table_partition_relationship():
    from pyatlan.model import assets

    obj = _column_base()
    obj["belongs_to_partition"] = "YES"
    result = Column.get_attributes(obj)
    assert result["attributes"]["parent_type"] is assets.TablePartition
    assert result["attributes"]["tablePartition"]["typeName"] == "TablePartition"


def test_column_unknown_table_type_falls_back_to_view():
    from pyatlan.model import assets

    obj = _column_base()
    obj["table_type"] = "STREAM"
    result = Column.get_attributes(obj)
    assert result["attributes"]["parent_type"] is assets.View
    assert result["attributes"]["view"]["typeName"] == "View"


def test_column_primary_foreign_nullable_partition_flags():
    obj = _column_base()
    obj.update(
        {
            "table_type": "TABLE",
            "primary_key": "YES",
            "foreign_key": "YES",
            "is_nullable": "NO",
            "is_partition": "YES",
            "partition_order": 2,
            "decimal_digits": 4,
            "numeric_precision": 10,
            "is_auto_increment": "NO",
        }
    )
    result = Column.get_attributes(obj)
    attrs = result["attributes"]
    custom = result["custom_attributes"]
    assert attrs["is_primary"] is True
    assert attrs["is_foreign"] is True
    assert attrs["is_nullable"] is False
    assert attrs["is_partition"] is True
    assert attrs["partition_order"] == 2
    assert attrs["precision"] == 4
    assert custom["numeric_precision"] == 10
    assert custom["is_auto_increment"] == "NO"
    assert custom["type_name"] == "VARCHAR"


# ---------------------------------------------------------------------------
# Function branches
# ---------------------------------------------------------------------------


def _function_base() -> Dict[str, Any]:
    return {
        "function_name": "FN",
        "argument_signature": "(a INT, b INT)",
        "function_definition": "RETURN 1",
        "is_external": "NO",
        "is_memoizable": "YES",
        "function_language": "SQL",
        "function_catalog": "DB",
        "function_schema": "SCH",
        "connection_qualified_name": CONN,
    }


def test_function_missing_required_raises():
    with pytest.raises(ValueError, match="Function name"):
        Function.get_attributes({})


def test_function_get_attributes_returns_scalar_for_non_table_data_type():
    obj = _function_base()
    obj["data_type"] = "NUMBER"
    obj["is_secure"] = "YES"
    obj["is_data_metric"] = "YES"
    result = Function.get_attributes(obj)
    attrs = result["attributes"]
    assert attrs["function_type"] == "Scalar"
    assert attrs["function_return_type"] == "NUMBER"
    assert attrs["function_is_secure"] is True
    assert attrs["function_is_external"] is False  # "NO"
    assert attrs["function_is_d_m_f"] is True
    assert attrs["function_is_memoizable"] is True
    assert attrs["function_arguments"] == ["a INT", " b INT"]
    assert attrs["qualified_name"] == f"{CONN}/DB/SCH/FN"


def test_function_get_attributes_returns_tabular_when_table_in_data_type():
    obj = _function_base()
    obj["data_type"] = "TABLE(...)"
    result = Function.get_attributes(obj)
    assert result["attributes"]["function_type"] == "Tabular"


def test_function_creator_constructs_attributes_from_schema_qualified_name():
    """Exercise Function.Attributes.create's fallback path for derived
    connection_qualified_name / database_name / schema_name."""
    schema_qn = "default/snowflake/1728518400/DB/SCH"
    fn = Function.creator(name="myfn", schema_qualified_name=schema_qn)
    assert fn is not None
    assert fn.attributes.name == "myfn"
    assert fn.attributes.qualified_name == f"{schema_qn}/myfn"
    assert fn.attributes.database_name == "DB"
    assert fn.attributes.schema_name == "SCH"


def test_function_creator_with_explicit_connection_qualified_name():
    schema_qn = "default/snowflake/1728518400/DB/SCH"
    fn = Function.creator(
        name="myfn",
        schema_qualified_name=schema_qn,
        schema_name="SCH",
        database_name="DB",
        database_qualified_name="default/snowflake/1728518400/DB",
        connection_qualified_name=CONN,
    )
    assert fn.attributes.connection_qualified_name == CONN
    assert fn.attributes.qualified_name == f"{schema_qn}/myfn"


# ---------------------------------------------------------------------------
# TagAttachment branches
# ---------------------------------------------------------------------------


def _tag_base() -> Dict[str, Any]:
    return {
        "tag_name": "PII",
        "tag_database": "TAGDB",
        "tag_schema": "TAGS",
        "object_database": "DB",
        "object_schema": "S",
        "connection_qualified_name": CONN,
    }


def test_tag_attachment_missing_field_caught_by_broad_except_returns_value_error():
    with pytest.raises(ValueError, match="Error creating TagAttachment Entity"):
        TagAttachment.get_attributes({})


def test_tag_attachment_basic_no_domain_uses_empty_object_qualified_name():
    obj = _tag_base()
    result = TagAttachment.get_attributes(obj)
    attrs = result["attributes"]
    assert attrs["name"] == "PII"
    assert attrs["tag_qualified_name"] == f"{CONN}/TAGDB/TAGS/PII"
    assert attrs["object_database_qualified_name"] == f"{CONN}/DB"
    assert attrs["object_schema_qualified_name"] == f"{CONN}/DB/S"
    assert attrs["object_database_name"] == "DB"
    assert attrs["object_schema_name"] == "S"
    # No domain -> object_qualified_name not assigned
    assert "object_qualified_name" not in attrs
    # No classification defs / mappedClassificationName -> empty json string
    assert attrs["mapped_classification_name"] == '""'


def test_tag_attachment_domain_database_uses_database_qualified_name():
    obj = _tag_base()
    obj.update({"domain": "DATABASE", "object_cat": "DB", "object_name": "MYDB"})
    result = TagAttachment.get_attributes(obj)
    assert result["attributes"]["object_qualified_name"] == f"{CONN}/DB/MYDB"


def test_tag_attachment_domain_schema_uses_schema_qualified_name():
    obj = _tag_base()
    obj.update(
        {
            "domain": "SCHEMA",
            "object_cat": "DB",
            "object_schema": "S",
            "object_name": "SMY",
        }
    )
    result = TagAttachment.get_attributes(obj)
    assert result["attributes"]["object_qualified_name"] == f"{CONN}/DB/S/SMY"


def test_tag_attachment_domain_table_uses_table_qualified_name():
    obj = _tag_base()
    obj.update(
        {
            "domain": "TABLE",
            "object_cat": "DB",
            "object_schema": "S",
            "object_name": "T",
        }
    )
    result = TagAttachment.get_attributes(obj)
    assert result["attributes"]["object_qualified_name"] == f"{CONN}/DB/S/T"


def test_tag_attachment_domain_column_uses_column_qualified_name():
    obj = _tag_base()
    obj.update(
        {
            "domain": "COLUMN",
            "object_cat": "DB",
            "object_schema": "S",
            "object_name": "T",
            "column_name": "C",
        }
    )
    result = TagAttachment.get_attributes(obj)
    assert result["attributes"]["object_qualified_name"] == f"{CONN}/DB/S/T/C"


def test_tag_attachment_classification_defs_picks_oldest_matching_by_name():
    obj = _tag_base()
    obj["classification_defs"] = [
        {"displayName": "PII", "name": "tag-uuid-newer", "createTime": 200},
        {"displayName": "PII", "name": "tag-uuid-older", "createTime": 100},
        {"displayName": "OTHER", "name": "ignored", "createTime": 50},
    ]
    result = TagAttachment.get_attributes(obj)
    # mapped_classification_name is JSON-encoded
    assert result["attributes"]["mapped_classification_name"] == '"tag-uuid-older"'


def test_tag_attachment_classification_defs_no_match_uses_mapped_classification_name():
    obj = _tag_base()
    obj["classification_defs"] = [
        {"displayName": "OTHER", "name": "x", "createTime": 1}
    ]
    obj["mappedClassificationName"] = "fallback-name"
    result = TagAttachment.get_attributes(obj)
    assert result["attributes"]["mapped_classification_name"] == '"fallback-name"'


def test_tag_attachment_creator_constructs_attributes_from_schema_qualified_name():
    schema_qn = "default/snowflake/1728518400/DB/SCH"
    ta = TagAttachment.creator(name="PII", schema_qualified_name=schema_qn)
    assert ta is not None
    assert ta.attributes.name == "PII"
    assert ta.attributes.qualified_name == f"{schema_qn}/PII"


def test_tag_attachment_creator_with_explicit_connection_qualified_name():
    schema_qn = "default/snowflake/1728518400/DB/SCH"
    ta = TagAttachment.creator(
        name="PII",
        schema_qualified_name=schema_qn,
        connection_qualified_name=CONN,
    )
    assert ta.attributes.connection_qualified_name == CONN


# ---------------------------------------------------------------------------
# Bug observations (skipped tests document the issue without modifying source)
# ---------------------------------------------------------------------------


def test_bug_table_is_partition_string_no_is_truthy():
    from pyatlan.model import assets

    obj = _table_base()
    obj["is_partition"] = "NO"
    obj["parent_table_name"] = "P"
    result = Table.get_attributes(obj)
    # Expected: not a partition; actual: classified as TablePartition.
    assert result["entity_class"] is not assets.TablePartition


def test_bug_tag_attachment_swallows_non_assertion_errors():
    """PR #1607 (BLDX-1169) narrowed `except Exception` to
    `except AssertionError` in `TagAttachment.get_attributes`. Other
    runtime errors (e.g. wrongly-typed `classification_defs`) now
    propagate with their original type instead of being re-wrapped as
    `ValueError("Error creating TagAttachment Entity: ...")`.
    """
    obj = _tag_base()
    # A string for classification_defs triggers AttributeError ('str'.get)
    # inside the body — must NOT be re-wrapped as ValueError.
    obj["classification_defs"] = "not-a-list"
    with pytest.raises((AttributeError, TypeError)):
        TagAttachment.get_attributes(obj)


def test_bug_function_argument_signature_without_parens_corrupts_arguments():
    obj = _function_base()
    obj["argument_signature"] = "AB"
    # Fix raises ValueError on malformed input rather than silently
    # producing a one-char field "B".
    with pytest.raises(ValueError, match="Malformed argument_signature"):
        Function.get_attributes(obj)
