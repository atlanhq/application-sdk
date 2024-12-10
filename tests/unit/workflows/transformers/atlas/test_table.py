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


def test_table_transformation(
    transformer: AtlasTransformer,
    raw_data: Dict[str, Any],
    expected_data: Dict[str, Any],
):
    """Test the transformation of regular tables"""
    workflow_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())

    transformed_data = transformer.transform_metadata(
        "TABLE", raw_data["tables"][0], workflow_id, run_id
    )

    assert transformed_data is not None
    expected_table = expected_data["tables"][0]

    # Test all table attributes
    assert transformed_data["typeName"] == "Table"
    assert (
        transformed_data["attributes"]["name"] == expected_table["attributes"]["name"]
    )
    assert (
        transformed_data["attributes"]["qualifiedName"]
        == expected_table["attributes"]["qualifiedName"]
    )
    assert (
        transformed_data["attributes"]["columnCount"]
        == expected_table["attributes"]["columnCount"]
    )
    assert (
        transformed_data["attributes"]["rowCount"]
        == expected_table["attributes"]["rowCount"]
    )
    assert (
        transformed_data["attributes"]["sizeBytes"]
        == expected_table["attributes"]["sizeBytes"]
    )

    # Test custom attributes
    assert (
        transformed_data["customAttributes"]["is_transient"]
        == expected_table["customAttributes"]["is_transient"]
    )
    assert (
        transformed_data["customAttributes"]["source_id"]
        == expected_table["customAttributes"]["source_id"]
    )


def test_view_transformation(
    transformer: AtlasTransformer,
    raw_data: Dict[str, Any],
    expected_data: Dict[str, Any],
):
    """Test the transformation of views"""
    workflow_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())

    transformed_data = transformer.transform_metadata(
        "VIEW", raw_data["views"][0], workflow_id, run_id
    )

    assert transformed_data is not None
    expected_view = expected_data["views"][0]

    # Test view-specific attributes
    assert transformed_data["typeName"] == "View"
    assert (
        transformed_data["attributes"]["definition"]
        == expected_view["attributes"]["definition"]
    )


def test_materialized_view_transformation(
    transformer: AtlasTransformer,
    raw_data: Dict[str, Any],
    expected_data: Dict[str, Any],
):
    """Test the transformation of materialized views"""
    workflow_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())

    transformed_data = transformer.transform_metadata(
        "TABLE", raw_data["materialized_views"][0], workflow_id, run_id
    )

    assert transformed_data is not None
    expected_mv = expected_data["materialized_views"][0]

    # Test materialized view specific attributes
    assert transformed_data["typeName"] == "MaterialisedView"
    assert (
        transformed_data["attributes"]["definition"]
        == expected_mv["attributes"]["definition"]
    )
