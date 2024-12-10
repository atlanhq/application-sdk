import json
import os
import uuid
from typing import Any, Dict

import pytest

from application_sdk.workflows.transformers.atlas import AtlasTransformer


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
    return AtlasTransformer(
        connector_name="snowflake", tenant_id="default", current_epoch=1728518400
    )


def test_regular_schema_transformation(
    transformer: AtlasTransformer,
    raw_data: Dict[str, Any],
    expected_data: Dict[str, Any],
):
    """Test the transformation of regular schemas"""
    workflow_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())

    transformed_data = transformer.transform_metadata(
        "SCHEMA", raw_data["schemas"][0], workflow_id, run_id
    )

    assert transformed_data is not None
    expected_schema = expected_data["schemas"][0]

    # Test basic attributes
    assert transformed_data["typeName"] == "Schema"
    assert (
        transformed_data["attributes"]["name"] == expected_schema["attributes"]["name"]
    )
    assert (
        transformed_data["attributes"]["qualifiedName"]
        == expected_schema["attributes"]["qualifiedName"]
    )
    assert (
        transformed_data["attributes"]["databaseName"]
        == expected_schema["attributes"]["databaseName"]
    )
    assert (
        transformed_data["attributes"]["databaseQualifiedName"]
        == expected_schema["attributes"]["databaseQualifiedName"]
    )

    # Test counts
    assert (
        transformed_data["attributes"]["tableCount"]
        == expected_schema["attributes"]["tableCount"]
    )
    assert (
        transformed_data["attributes"]["viewsCount"]
        == expected_schema["attributes"]["viewsCount"]
    )

    # Test custom attributes
    assert (
        transformed_data["customAttributes"]["catalog_id"]
        == expected_schema["customAttributes"]["catalog_id"]
    )
    assert (
        transformed_data["customAttributes"]["is_managed_access"]
        == expected_schema["customAttributes"]["is_managed_access"]
    )


def test_schema_with_metadata(
    transformer: AtlasTransformer,
    raw_data: Dict[str, Any],
    expected_data: Dict[str, Any],
):
    """Test schema transformation with additional metadata"""
    workflow_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())

    transformed_data = transformer.transform_metadata(
        "SCHEMA", raw_data["schemas_with_metadata"][0], workflow_id, run_id
    )

    assert transformed_data is not None
    expected_schema = expected_data["schemas_with_metadata"][0]

    # Test metadata attributes
    assert (
        json.loads(transformed_data["attributes"]["description"])
        == expected_schema["attributes"]["description"]
    )
    assert (
        transformed_data["attributes"]["sourceCreatedBy"]
        == expected_schema["attributes"]["sourceCreatedBy"]
    )
    assert (
        transformed_data["attributes"]["sourceCreatedAt"].timestamp()
        == expected_schema["attributes"]["sourceCreatedAt"]
    )
    assert (
        transformed_data["attributes"]["sourceUpdatedAt"].timestamp()
        == expected_schema["attributes"]["sourceUpdatedAt"]
    )


def test_schema_invalid_data(transformer: AtlasTransformer):
    """Test schema transformation with invalid data"""
    workflow_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())

    # Test missing required fields
    invalid_data = {"connection_qualified_name": "default/snowflake/1728518400"}

    transformed_data = transformer.transform_metadata(
        "SCHEMA", invalid_data, workflow_id, run_id
    )

    assert transformed_data is None
