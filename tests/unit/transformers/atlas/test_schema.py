import json
import os
import uuid
from typing import Any, Dict, List

import pytest

from application_sdk.transformers.atlas import AtlasTransformer


@pytest.fixture
def resources_dir():
    return os.path.join(os.path.dirname(__file__), "resources")


@pytest.fixture
def raw_data(resources_dir: str) -> Dict[str, Any]:
    with open(os.path.join(resources_dir, "raw_schemas.json")) as f:
        return json.load(f)


@pytest.fixture
def expected_data(resources_dir: str) -> Dict[str, Any]:
    with open(os.path.join(resources_dir, "transformed_schemas.json")) as f:
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
        assert (
            transformed_data[attr_type][attr] == expected_data[attr_type][attr]
        ), f"Mismatch in {'custom ' if is_custom else ''}{attr}"


def test_regular_schema_transformation(
    transformer: AtlasTransformer,
    raw_data: Dict[str, Any],
    expected_data: Dict[str, Any],
):
    """Test the transformation of regular schemas"""
    transformed_data = transformer.transform_row(
        "SCHEMA",
        raw_data["regular_schema"],
        "test_workflow_id",
        "test_run_id",
        connection_name="test-connection",
        connection_qualified_name="default/snowflake/1728518400",
    )

    assert transformed_data is not None
    expected_schema = expected_data["regular_schema"]

    # Basic type assertion
    assert transformed_data["typeName"] == "Schema"

    # Standard attributes verification
    standard_attributes = [
        "name",
        "qualifiedName",
        "databaseName",
        "databaseQualifiedName",
        "tableCount",
        "viewsCount",
        "lastSyncRun",
        "lastSyncWorkflowName",
    ]
    assert_attributes(transformed_data, expected_schema, standard_attributes)

    # Custom attributes verification
    custom_attributes = ["source_id", "catalog_id", "is_managed_access"]
    assert_attributes(
        transformed_data, expected_schema, custom_attributes, is_custom=True
    )

    # Direct comparison for description since it's processed text
    assert (
        transformed_data["attributes"]["description"]
        == expected_schema["attributes"]["description"]
    )

    # Special handling for timestamps
    assert (
        transformed_data["attributes"]["sourceCreatedAt"].timestamp()
        == expected_schema["attributes"]["sourceCreatedAt"]
    )
    assert (
        transformed_data["attributes"]["sourceUpdatedAt"].timestamp()
        == expected_schema["attributes"]["sourceUpdatedAt"]
    )

    # Regular metadata attribute
    assert (
        transformed_data["attributes"]["sourceCreatedBy"]
        == expected_schema["attributes"]["sourceCreatedBy"]
    )


def test_schema_invalid_data(transformer: AtlasTransformer):
    """Test schema transformation with invalid data"""
    workflow_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())

    # Test missing required fields
    invalid_data = {"connection_qualified_name": "default/snowflake/1728518400"}

    transformed_data = transformer.transform_row(
        "SCHEMA",
        invalid_data,
        workflow_id,
        run_id,
        connection_name="test-connection",
        connection_qualified_name="default/snowflake/1728518400",
    )

    assert transformed_data is None
