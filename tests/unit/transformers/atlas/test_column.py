import json
import os
from typing import Any, Dict, List

import pytest

from application_sdk.transformers.atlas import AtlasTransformer

column_attributes = [
    "qualifiedName",
    "name",
    "tenantId",
    "connectorName",
    "connectionName",
    "connectionQualifiedName",
    "lastSyncWorkflowName",
    "lastSyncRun",
    "databaseName",
    "databaseQualifiedName",
    "schemaName",
    "schemaQualifiedName",
    "tableName",
    "tableQualifiedName",
    "viewName",
    "viewQualifiedName",
    "dataType",
    "order",
    "isPartition",
    "isPrimary",
    "isForeign",
    "isNullable",
    "numericScale",
    "maxLength",
]


@pytest.fixture
def resources_dir():
    return os.path.join(os.path.dirname(__file__), "resources")


@pytest.fixture
def raw_data(resources_dir: str) -> Dict[str, Any]:
    with open(os.path.join(resources_dir, "raw_columns.json")) as f:
        return json.load(f)


@pytest.fixture
def expected_data(resources_dir: str) -> Dict[str, Any]:
    with open(os.path.join(resources_dir, "transformed_columns.json")) as f:
        return json.load(f)


@pytest.fixture
def transformer():
    return AtlasTransformer(connector_name="snowflake", tenant_id="default")


def assert_attributes(
    transformed_data: Dict[str, Any],
    expected_data: Dict[str, Any],
    attributes: List[str],
    is_custom: bool = False,
):
    attr_type = "customAttributes" if is_custom else "attributes"
    for attr in attributes:
        if (
            attr not in transformed_data[attr_type]
            and attr not in expected_data[attr_type]
        ):
            continue
        assert (
            transformed_data[attr_type][attr] == expected_data[attr_type][attr]
        ), f"Mismatch in {'custom ' if is_custom else ''}{attr}"


def test_regular_column_transformation(
    transformer: AtlasTransformer,
    raw_data: Dict[str, Any],
    expected_data: Dict[str, Any],
):
    """Test the transformation of regular table columns"""

    transformed_data = transformer.transform_row(
        "COLUMN",
        raw_data["regular_columns"],
        "test_workflow_id",
        "test_run_id",
        connection_name="test-connection",
        connection_qualified_name="default/snowflake/1728518400",
    )

    assert transformed_data is not None
    expected_column = expected_data["regular_columns"]

    assert transformed_data["typeName"] == "Column"

    # Standard attributes verification
    standard_attributes = [
        "name",
        "qualifiedName",
        "dataType",
        "order",
        "isNullable",
        "isPrimary",
        "isForeign",
        "maxLength",
        "precision",
        "numericScale",
        "lastSyncRun",
        "lastSyncWorkflowName",
    ]
    assert_attributes(transformed_data, expected_column, standard_attributes)


def test_view_column_transformation(
    transformer: AtlasTransformer,
    raw_data: Dict[str, Any],
    expected_data: Dict[str, Any],
):
    """Test the transformation of view columns"""

    transformed_data = transformer.transform_row(
        "COLUMN",
        raw_data["view_columns"],
        "test_workflow_id",
        "test_run_id",
        connection_name="test-connection",
        connection_qualified_name="default/snowflake/1728518400",
    )

    assert transformed_data is not None
    expected_column = expected_data["view_columns"]

    standard_attributes = ["name", "dataType", "numericScale"]
    assert_attributes(transformed_data, expected_column, standard_attributes)

    # Test view relationship
    assert "view" in transformed_data["attributes"]
    assert (
        transformed_data["attributes"]["view"]["uniqueAttributes"]["qualifiedName"]
        == expected_column["attributes"]["view"]["uniqueAttributes"]["qualifiedName"]
    )


def test_materialized_view_column_transformation(
    transformer: AtlasTransformer,
    raw_data: Dict[str, Any],
    expected_data: Dict[str, Any],
):
    """Test the transformation of materialized view columns"""

    transformed_data = transformer.transform_row(
        "COLUMN",
        raw_data["materialized_view_columns"],
        "test_workflow_id",
        "test_run_id",
        connection_name="test-connection",
        connection_qualified_name="default/snowflake/1728518400",
    )

    assert transformed_data is not None
    expected_column = expected_data["materialized_view_columns"]

    standard_attributes = ["name", "dataType", "numericScale"]
    assert_attributes(transformed_data, expected_column, standard_attributes)

    # Test materialized view relationship
    assert "materialisedView" in transformed_data["attributes"]
    assert (
        transformed_data["attributes"]["materialisedView"]["uniqueAttributes"][
            "qualifiedName"
        ]
        == expected_column["attributes"]["materialisedView"]["uniqueAttributes"][
            "qualifiedName"
        ]
    )


def test_column_with_view_parent(
    transformer: AtlasTransformer,
    raw_data: Dict[str, Any],
    expected_data: Dict[str, Any],
):
    """Test column transformation with custom attributes"""

    transformed_data = transformer.transform_row(
        "COLUMN",
        raw_data["column_with_view_parent"],
        "test_workflow_id",
        "test_run_id",
        connection_name="test-connection",
        connection_qualified_name="default/postgres/1741637459",
    )

    assert transformed_data is not None
    expected_column = expected_data["column_with_view_parent"]

    custom_attributes = [
        "ordinal_position",
        "is_self_referencing",
        "type_name",
        "is_auto_increment",
        "is_generated",
        "column_size",
    ]

    assert_attributes(
        transformed_data, expected_column, custom_attributes, is_custom=True
    )
    assert_attributes(transformed_data, expected_column, column_attributes)
    assert "lastSyncRunAt" in transformed_data["attributes"]
    assert transformed_data["attributes"]["lastSyncRunAt"] is not None

    # Test view relationship
    assert "view" in transformed_data["attributes"]
    assert (
        transformed_data["attributes"]["view"]["uniqueAttributes"]["qualifiedName"]
        == expected_column["attributes"]["view"]["uniqueAttributes"]["qualifiedName"]
    )


def test_column_with_materialized_view_parent(
    transformer: AtlasTransformer,
    raw_data: Dict[str, Any],
    expected_data: Dict[str, Any],
):
    """Test column transformation with custom attributes"""

    transformed_data = transformer.transform_row(
        "COLUMN",
        raw_data["column_with_materialized_view_parent"],
        "test_workflow_id",
        "test_run_id",
        connection_name="test-connection",
        connection_qualified_name="default/postgres/1741637459",
    )

    assert transformed_data is not None
    expected_column = expected_data["column_with_materialized_view_parent"]

    custom_attributes = [
        "ordinal_position",
        "is_self_referencing",
        "type_name",
        "is_auto_increment",
        "is_generated",
        "column_size",
    ]
    assert_attributes(
        transformed_data, expected_column, custom_attributes, is_custom=True
    )
    assert_attributes(transformed_data, expected_column, column_attributes)
    assert "lastSyncRunAt" in transformed_data["attributes"]
    assert transformed_data["attributes"]["lastSyncRunAt"] is not None

    # Test materialized view relationship
    assert "materialisedView" in transformed_data["attributes"]
    assert (
        transformed_data["attributes"]["materialisedView"]["uniqueAttributes"][
            "qualifiedName"
        ]
        == expected_column["attributes"]["materialisedView"]["uniqueAttributes"][
            "qualifiedName"
        ]
    )


def test_column_with_foreign_table_parent(
    transformer: AtlasTransformer,
    raw_data: Dict[str, Any],
    expected_data: Dict[str, Any],
):
    """Test column transformation with custom attributes"""

    transformed_data = transformer.transform_row(
        "COLUMN",
        raw_data["column_with_foreign_table_parent"],
        "test_workflow_id",
        "test_run_id",
        connection_name="test-connection",
        connection_qualified_name="default/postgres/1741637459",
    )

    assert transformed_data is not None
    expected_column = expected_data["column_with_foreign_table_parent"]

    custom_attributes = [
        "ordinal_position",
        "is_self_referencing",
        "type_name",
        "is_auto_increment",
        "is_generated",
        "column_size",
    ]
    assert_attributes(
        transformed_data, expected_column, custom_attributes, is_custom=True
    )
    assert_attributes(transformed_data, expected_column, column_attributes)
    assert "lastSyncRunAt" in transformed_data["attributes"]
    assert transformed_data["attributes"]["lastSyncRunAt"] is not None

    # Test materialized view relationship
    assert "table" in transformed_data["attributes"]
    assert (
        transformed_data["attributes"]["table"]["uniqueAttributes"]["qualifiedName"]
        == expected_column["attributes"]["table"]["uniqueAttributes"]["qualifiedName"]
    )


def test_column_with_partitioned_table_parent(
    transformer: AtlasTransformer,
    raw_data: Dict[str, Any],
    expected_data: Dict[str, Any],
):
    """Test column transformation with custom attributes"""

    transformed_data = transformer.transform_row(
        "COLUMN",
        raw_data["column_partitioned_table"],
        "test_workflow_id",
        "test_run_id",
        connection_name="test-connection",
        connection_qualified_name="default/postgres/1741637459",
    )

    assert transformed_data is not None
    expected_column = expected_data["column_partitioned_table"]

    custom_attributes = [
        "ordinal_position",
        "is_self_referencing",
        "type_name",
        "is_auto_increment",
        "is_generated",
        "column_size",
    ]
    assert_attributes(
        transformed_data, expected_column, custom_attributes, is_custom=True
    )
    assert_attributes(transformed_data, expected_column, column_attributes)
    assert "lastSyncRunAt" in transformed_data["attributes"]
    assert transformed_data["attributes"]["lastSyncRunAt"] is not None

    # Test materialized view relationship
    assert "tablePartition" in transformed_data["attributes"]
    assert (
        transformed_data["attributes"]["tablePartition"]["uniqueAttributes"][
            "qualifiedName"
        ]
        == expected_column["attributes"]["tablePartition"]["uniqueAttributes"][
            "qualifiedName"
        ]
    )


def test_column_variation_1(
    transformer: AtlasTransformer,
    raw_data: Dict[str, Any],
    expected_data: Dict[str, Any],
):
    """Test column transformation with custom attributes"""

    transformed_data = transformer.transform_row(
        "COLUMN",
        raw_data["column_variation_1"],
        "test_workflow_id",
        "test_run_id",
        connection_name="test-connection",
        connection_qualified_name="default/postgres/1741637459",
    )

    assert transformed_data is not None
    expected_column = expected_data["column_variation_1"]

    custom_attributes = [
        "ordinal_position",
        "is_self_referencing",
        "type_name",
        "is_auto_increment",
        "is_generated",
        "column_size",
    ]
    assert_attributes(
        transformed_data, expected_column, custom_attributes, is_custom=True
    )
    assert_attributes(transformed_data, expected_column, column_attributes)
    assert "lastSyncRunAt" in transformed_data["attributes"]
    assert transformed_data["attributes"]["lastSyncRunAt"] is not None

    # Test materialized view relationship
    assert "table" in transformed_data["attributes"]
    assert (
        transformed_data["attributes"]["table"]["uniqueAttributes"]["qualifiedName"]
        == expected_column["attributes"]["table"]["uniqueAttributes"]["qualifiedName"]
    )


def test_column_variation_2(
    transformer: AtlasTransformer,
    raw_data: Dict[str, Any],
    expected_data: Dict[str, Any],
):
    """Test column transformation with custom attributes"""

    transformed_data = transformer.transform_row(
        "COLUMN",
        raw_data["column_variation_2"],
        "test_workflow_id",
        "test_run_id",
        connection_name="test-connection",
        connection_qualified_name="default/postgres/1741637459",
    )

    assert transformed_data is not None
    expected_column = expected_data["column_variation_2"]

    custom_attributes = [
        "ordinal_position",
        "is_self_referencing",
        "type_name",
        "is_auto_increment",
        "is_generated",
        "column_size",
    ]
    assert_attributes(
        transformed_data, expected_column, custom_attributes, is_custom=True
    )
    assert_attributes(transformed_data, expected_column, column_attributes)
    assert "lastSyncRunAt" in transformed_data["attributes"]
    assert transformed_data["attributes"]["lastSyncRunAt"] is not None

    # Test materialized view relationship
    assert "table" in transformed_data["attributes"]
    assert (
        transformed_data["attributes"]["table"]["uniqueAttributes"]["qualifiedName"]
        == expected_column["attributes"]["table"]["uniqueAttributes"]["qualifiedName"]
    )


def test_column_with_custom_attributes(
    transformer: AtlasTransformer,
    raw_data: Dict[str, Any],
    expected_data: Dict[str, Any],
):
    """Test column transformation with custom attributes"""

    transformed_data = transformer.transform_row(
        "COLUMN",
        raw_data["columns_with_custom_attrs"],
        "test_workflow_id",
        "test_run_id",
        connection_name="test-connection",
        connection_qualified_name="default/snowflake/1728518400",
    )

    assert transformed_data is not None
    expected_column = expected_data["columns_with_custom_attrs"]

    custom_attributes = [
        "is_self_referencing",
        "source_id",
        "is_auto_increment",
        "is_generated",
        "numeric_precision",
    ]
    assert_attributes(
        transformed_data, expected_column, custom_attributes, is_custom=True
    )
    # Test table relationship
    assert "table" in transformed_data["attributes"]
    assert (
        transformed_data["attributes"]["table"]["uniqueAttributes"]["qualifiedName"]
        == expected_column["attributes"]["table"]["uniqueAttributes"]["qualifiedName"]
    )


def test_column_invalid_data(transformer: AtlasTransformer):
    """Test column transformation with invalid data"""

    invalid_data = {"connection_qualified_name": "default/snowflake/1728518400"}
    transformed_data = transformer.transform_row(
        "COLUMN",
        invalid_data,
        "test_workflow_id",
        "test_run_id",
        connection_name="test-connection",
        connection_qualified_name="default/snowflake/1728518400",
    )

    assert transformed_data is None
