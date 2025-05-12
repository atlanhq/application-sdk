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
    with open(os.path.join(resources_dir, "raw_databases.json")) as f:
        return json.load(f)


@pytest.fixture
def expected_data(resources_dir: str) -> Dict[str, Any]:
    with open(os.path.join(resources_dir, "transformed_databases.json")) as f:
        return json.load(f)


@pytest.fixture
def transformer():
    return AtlasTransformer(connector_name="snowflake", tenant_id="default")


def assert_attributes(
    transformed_data: Dict[str, Any],
    expected_db: Dict[str, Any],
    attributes: List[str],
    is_custom: bool = False,
):
    attr_type = "customAttributes" if is_custom else "attributes"
    for attr in attributes:
        assert (
            transformed_data[attr_type][attr] == expected_db[attr_type][attr]
        ), f"Mismatch in {'custom ' if is_custom else ''}{attr}"


def test_database_transformation(
    transformer: AtlasTransformer,
    raw_data: Dict[str, Any],
    expected_data: Dict[str, Any],
):
    """Test the transformation of raw database metadata"""

    transformed_data = transformer.transform_row(
        "DATABASE",
        raw_data["regular_database"],
        "test_workflow_id",
        "test_run_id",
        connection_name="test-connection",
        connection_qualified_name="default/snowflake/1728518400",
    )

    assert transformed_data is not None
    expected_db = expected_data["regular_database"]

    # Basic type assertion
    assert transformed_data["typeName"] == "Database"

    # Standard attributes verification
    standard_attributes = [
        "name",
        "qualifiedName",
        "schemaCount",
        "connectionQualifiedName",
        "lastSyncRun",
        "lastSyncWorkflowName",
        "sourceCreatedBy",
    ]
    assert_attributes(transformed_data, expected_db, standard_attributes)

    # Custom attributes verification
    custom_attributes = ["source_id"]
    assert_attributes(transformed_data, expected_db, custom_attributes, is_custom=True)


def test_database_invalid_data(transformer: AtlasTransformer):
    """Test database transformation with invalid data"""
    workflow_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())

    # Test missing required fields
    invalid_data = {"connection_qualified_name": "default/snowflake/1728518400"}

    transformed_data = transformer.transform_row(
        "DATABASE",
        invalid_data,
        workflow_id,
        run_id,
        connection_name="test-connection",
        connection_qualified_name="default/snowflake/1728518400",
    )

    assert transformed_data is None
