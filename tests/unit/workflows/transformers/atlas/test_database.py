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
    with open(os.path.join(resources_dir, "raw_databases.json")) as f:
        return json.load(f)


@pytest.fixture
def expected_data(resources_dir: str) -> Dict[str, Any]:
    with open(os.path.join(resources_dir, "transformed_databases.json")) as f:
        return json.load(f)


@pytest.fixture
def transformer():
    return AtlasTransformer(
        connector_name="snowflake", tenant_id="default", current_epoch=1728518400
    )


def test_database_transformation(
    transformer: AtlasTransformer,
    raw_data: Dict[str, Any],
    expected_data: Dict[str, Any],
):
    """Test the transformation of raw database metadata"""
    workflow_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())

    transformed_data = transformer.transform_metadata(
        "DATABASE", raw_data["databases"][0], workflow_id, run_id
    )

    assert transformed_data is not None
    expected_db = expected_data["databases"][0]

    # Test all database attributes
    assert transformed_data["typeName"] == "Database"
    assert transformed_data["attributes"]["name"] == expected_db["attributes"]["name"]
    assert (
        transformed_data["attributes"]["qualifiedName"]
        == expected_db["attributes"]["qualifiedName"]
    )
    assert (
        transformed_data["attributes"]["schemaCount"]
        == expected_db["attributes"]["schemaCount"]
    )
    assert (
        transformed_data["attributes"]["connectionQualifiedName"]
        == expected_db["attributes"]["connectionQualifiedName"]
    )
    assert transformed_data["attributes"]["lastSyncRun"] == run_id
    assert transformed_data["attributes"]["lastSyncWorkflowName"] == workflow_id

    # Test custom attributes
    assert (
        transformed_data["customAttributes"]["source_id"]
        == expected_db["customAttributes"]["source_id"]
    )


def test_database_invalid_data(transformer: AtlasTransformer):
    """Test database transformation with invalid data"""
    workflow_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())

    # Test missing required fields
    invalid_data = {"connection_qualified_name": "default/snowflake/1728518400"}

    transformed_data = transformer.transform_metadata(
        "DATABASE", invalid_data, workflow_id, run_id
    )

    assert transformed_data is None
