import json
import os
from typing import Any, Dict, List

import pytest

from application_sdk.transformers.atlas import AtlasTransformer


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
    return AtlasTransformer(
        connector_name="snowflake", tenant_id="default", current_epoch=1728518400
    )


def assert_attributes(
    transformed_data: Dict[str, Any],
    expected_data: Dict[str, Any],
    attributes: List[str],
    is_custom: bool = False,
):
    attr_type = "customAttributes" if is_custom else "attributes"
    for attr in attributes:
        assert (
            transformed_data[attr_type][attr] == expected_data[attr_type][attr]
        ), f"Mismatch in {'custom ' if is_custom else ''}{attr}"


def test_table_transformation(
    transformer: AtlasTransformer,
    raw_data: Dict[str, Any],
    expected_data: Dict[str, Any],
):
    """Test the transformation of regular tables"""

    transformed_data = transformer.transform_metadata(
        "TABLE", raw_data["regular_table"], "test_workflow_id", "test_run_id"
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

    # Special handling for description as it's JSON
    assert (
        json.loads(transformed_data["attributes"]["description"])
        == expected_table["attributes"]["description"]
    )


def test_view_transformation(
    transformer: AtlasTransformer,
    raw_data: Dict[str, Any],
    expected_data: Dict[str, Any],
):
    """Test the transformation of views"""

    transformed_data = transformer.transform_metadata(
        "VIEW", raw_data["regular_view"], "test_workflow_id", "test_run_id"
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

    transformed_data = transformer.transform_metadata(
        "TABLE",
        raw_data["regular_materialized_view"],
        "test_workflow_id",
        "test_run_id",
    )

    assert transformed_data is not None
    expected_mv = expected_data["regular_materialized_view"]

    assert transformed_data["typeName"] == "MaterialisedView"
    assert_attributes(transformed_data, expected_mv, ["definition"])
