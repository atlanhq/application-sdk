import json
import os
from typing import Any, Dict, List

import pytest

from application_sdk.transformers.atlas import AtlasTransformer

table_attributes = [
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
    "columnCount",
    "rowCount",
    "sizeBytes",
    "externalLocation",
    "externalLocationRegion",
    "externalLocationFormat",
]
table_custom_attributes = [
    "table_type",
    "is_insertable_into",
    "number_columns_in_part_key",
    "columns_participating_in_part_key",
    "is_typed",
    "engine",
]


@pytest.fixture
def resources_dir():
    return os.path.join(os.path.dirname(__file__), "resources")


@pytest.fixture
def raw_data(resources_dir: str) -> Dict[str, Any]:
    with open(os.path.join(resources_dir, "raw_tables.json")) as f:
        return json.load(f)


@pytest.fixture
def expected_data(resources_dir: str) -> Dict[str, Any]:
    with open(os.path.join(resources_dir, "transformed_tables.json")) as f:
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


def test_table_transformation(
    transformer: AtlasTransformer,
    raw_data: Dict[str, Any],
    expected_data: Dict[str, Any],
):
    """Test the transformation of regular tables"""

    transformed_data = transformer.transform_row(
        "TABLE",
        raw_data["regular_table"],
        "test_workflow_id",
        "test_run_id",
        connection_name="test-connection",
        connection_qualified_name="default/snowflake/1728518400",
    )

    assert transformed_data is not None
    expected_table = expected_data["regular_table"]

    # Basic type assertion
    assert transformed_data["typeName"] == "Table"

    # Standard attributes verification
    standard_attributes = [
        "name",
        "qualifiedName",
        "columnCount",
        "rowCount",
        "sizeBytes",
        "databaseName",
        "schemaName",
        "databaseQualifiedName",
        "schemaQualifiedName",
        "connectionQualifiedName",
        "sourceCreatedBy",
        "lastSyncRun",
        "lastSyncWorkflowName",
    ]
    assert_attributes(transformed_data, expected_table, standard_attributes)

    # Custom attributes verification
    custom_attributes = ["is_transient", "source_id"]
    assert_attributes(
        transformed_data, expected_table, custom_attributes, is_custom=True
    )

    # Direct comparison for description since it's processed text
    assert (
        transformed_data["attributes"]["description"]
        == expected_table["attributes"]["description"]
    )


def test_view_transformation(
    transformer: AtlasTransformer,
    raw_data: Dict[str, Any],
    expected_data: Dict[str, Any],
):
    """Test the transformation of views"""

    transformed_data = transformer.transform_row(
        "VIEW",
        raw_data["regular_view"],
        "test_workflow_id",
        "test_run_id",
        connection_name="test-connection",
        connection_qualified_name="default/snowflake/1728518400",
    )

    assert transformed_data is not None
    expected_view = expected_data["regular_view"]

    assert transformed_data["typeName"] == "View"
    assert_attributes(transformed_data, expected_view, ["definition"])


def test_materialized_view_transformation(
    transformer: AtlasTransformer,
    raw_data: Dict[str, Any],
    expected_data: Dict[str, Any],
):
    """Test the transformation of materialized views"""

    transformed_data = transformer.transform_row(
        "TABLE",
        raw_data["regular_materialized_view"],
        "test_workflow_id",
        "test_run_id",
        connection_name="test-connection",
        connection_qualified_name="default/snowflake/1728518400",
    )

    assert transformed_data is not None
    expected_mv = expected_data["regular_materialized_view"]

    assert transformed_data["typeName"] == "MaterialisedView"
    assert_attributes(transformed_data, expected_mv, ["definition"])


def test_table_variation_1_transformation(
    transformer: AtlasTransformer,
    raw_data: Dict[str, Any],
    expected_data: Dict[str, Any],
):
    """Test the transformation of regular tables"""

    transformed_data = transformer.transform_row(
        "TABLE",
        raw_data["table_variation_1"],
        "test_workflow_id",
        "test_run_id",
        connection_name="test-connection",
        connection_qualified_name="default/postgres/1728518400",
    )

    assert transformed_data is not None
    expected_table = expected_data["table_variation_1"]

    # Basic type assertion
    assert transformed_data["typeName"] == "Table"

    assert_attributes(transformed_data, expected_table, table_attributes)
    assert_attributes(
        transformed_data, expected_table, table_custom_attributes, is_custom=True
    )

    assert "atlanSchema" in transformed_data["attributes"]
    assert transformed_data["attributes"]["atlanSchema"]["typeName"] == "Schema"
    assert (
        transformed_data["attributes"]["atlanSchema"]["uniqueAttributes"][
            "qualifiedName"
        ]
        == expected_table["attributes"]["atlanSchema"]["uniqueAttributes"][
            "qualifiedName"
        ]
    )


def test_view_variation_1_transformation(
    transformer: AtlasTransformer,
    raw_data: Dict[str, Any],
    expected_data: Dict[str, Any],
):
    """Test the transformation of regular tables"""

    transformed_data = transformer.transform_row(
        "TABLE",
        raw_data["view_variation_1"],
        "test_workflow_id",
        "test_run_id",
        connection_name="postgres",
        connection_qualified_name="default/postgres/1728518400",
    )

    assert transformed_data is not None
    expected_table = expected_data["view_variation_1"]

    # Basic type assertion
    assert transformed_data["typeName"] == "View"

    assert_attributes(transformed_data, expected_table, table_attributes)
    assert_attributes(
        transformed_data, expected_table, table_custom_attributes, is_custom=True
    )

    assert "atlanSchema" in transformed_data["attributes"]
    assert transformed_data["attributes"]["atlanSchema"]["typeName"] == "Schema"
    assert (
        transformed_data["attributes"]["atlanSchema"]["uniqueAttributes"][
            "qualifiedName"
        ]
        == expected_table["attributes"]["atlanSchema"]["uniqueAttributes"][
            "qualifiedName"
        ]
    )


def test_materialized_view_variation_1_transformation(
    transformer: AtlasTransformer,
    raw_data: Dict[str, Any],
    expected_data: Dict[str, Any],
):
    """Test the transformation of regular tables"""

    transformed_data = transformer.transform_row(
        "TABLE",
        raw_data["materialized_view_variation_1"],
        "test_workflow_id",
        "test_run_id",
        connection_name="postgres",
        connection_qualified_name="default/postgres/1728518400",
    )

    assert transformed_data is not None
    expected_table = expected_data["materialized_view_variation_1"]

    # Basic type assertion
    assert transformed_data["typeName"] == "MaterialisedView"

    assert_attributes(transformed_data, expected_table, table_attributes)
    assert_attributes(
        transformed_data, expected_table, table_custom_attributes, is_custom=True
    )

    assert "atlanSchema" in transformed_data["attributes"]
    assert transformed_data["attributes"]["atlanSchema"]["typeName"] == "Schema"
    assert (
        transformed_data["attributes"]["atlanSchema"]["uniqueAttributes"][
            "qualifiedName"
        ]
        == expected_table["attributes"]["atlanSchema"]["uniqueAttributes"][
            "qualifiedName"
        ]
    )


def test_table_partition_transformation(
    transformer: AtlasTransformer,
    raw_data: Dict[str, Any],
    expected_data: Dict[str, Any],
):
    """Test the transformation of regular tables"""

    transformed_data = transformer.transform_row(
        "TABLE",
        raw_data["partitioned_table"],
        "test_workflow_id",
        "test_run_id",
        connection_name="postgres",
        connection_qualified_name="default/postgres/1728518400",
    )

    assert transformed_data is not None
    expected_table = expected_data["partitioned_table"]

    # Basic type assertion
    assert transformed_data["typeName"] == "TablePartition"

    assert_attributes(transformed_data, expected_table, table_attributes)
    assert_attributes(
        transformed_data, expected_table, table_custom_attributes, is_custom=True
    )

    assert "parentTable" in transformed_data["attributes"]
    assert transformed_data["attributes"]["parentTable"]["typeName"] == "Table"
    assert (
        transformed_data["attributes"]["parentTable"]["uniqueAttributes"][
            "qualifiedName"
        ]
        == expected_table["attributes"]["parentTable"]["uniqueAttributes"][
            "qualifiedName"
        ]
    )
