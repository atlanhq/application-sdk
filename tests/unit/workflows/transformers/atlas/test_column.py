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
    with open(os.path.join(resources_dir, "raw_columns.json")) as f:
        return json.load(f)


@pytest.fixture
def expected_data(resources_dir: str) -> Dict[str, Any]:
    with open(os.path.join(resources_dir, "transformed_columns.json")) as f:
        return json.load(f)


@pytest.fixture
def transformer():
    return AtlasTransformer(
        connector_name="snowflake", tenant_id="default", current_epoch=1728518400
    )


def test_regular_column_transformation(
    transformer: AtlasTransformer,
    raw_data: Dict[str, Any],
    expected_data: Dict[str, Any],
):
    """Test the transformation of regular table columns"""
    workflow_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())

    transformed_data = transformer.transform_metadata(
        "COLUMN", raw_data["regular_columns"][0], workflow_id, run_id
    )

    assert transformed_data is not None
    expected_column = expected_data["regular_columns"][0]

    # Test basic attributes
    assert transformed_data["typeName"] == "Column"
    assert (
        transformed_data["attributes"]["name"] == expected_column["attributes"]["name"]
    )
    assert (
        transformed_data["attributes"]["qualifiedName"]
        == expected_column["attributes"]["qualifiedName"]
    )
    assert (
        transformed_data["attributes"]["dataType"]
        == expected_column["attributes"]["dataType"]
    )
    assert (
        transformed_data["attributes"]["order"]
        == expected_column["attributes"]["order"]
    )

    # Test nullable and constraints
    assert (
        transformed_data["attributes"]["isNullable"]
        == expected_column["attributes"]["isNullable"]
    )
    assert (
        transformed_data["attributes"]["isPrimary"]
        == expected_column["attributes"]["isPrimary"]
    )
    assert (
        transformed_data["attributes"]["isForeign"]
        == expected_column["attributes"]["isForeign"]
    )

    # Test data type specifics
    assert (
        transformed_data["attributes"]["maxLength"]
        == expected_column["attributes"]["maxLength"]
    )
    assert (
        transformed_data["attributes"]["precision"]
        == expected_column["attributes"]["precision"]
    )
    assert (
        transformed_data["attributes"]["numericScale"]
        == expected_column["attributes"]["numericScale"]
    )


def test_view_column_transformation(
    transformer: AtlasTransformer,
    raw_data: Dict[str, Any],
    expected_data: Dict[str, Any],
):
    """Test the transformation of view columns"""
    workflow_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())

    transformed_data = transformer.transform_metadata(
        "COLUMN", raw_data["view_columns"][0], workflow_id, run_id
    )

    assert transformed_data is not None
    expected_column = expected_data["view_columns"][0]

    # Test view column specific attributes
    assert (
        transformed_data["attributes"]["name"] == expected_column["attributes"]["name"]
    )
    assert (
        transformed_data["attributes"]["dataType"]
        == expected_column["attributes"]["dataType"]
    )
    assert (
        transformed_data["attributes"]["precision"]
        == expected_column["attributes"]["precision"]
    )
    assert (
        transformed_data["attributes"]["numericScale"]
        == expected_column["attributes"]["numericScale"]
    )

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
    workflow_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())

    transformed_data = transformer.transform_metadata(
        "COLUMN", raw_data["materialized_view_columns"][0], workflow_id, run_id
    )

    assert transformed_data is not None
    expected_column = expected_data["materialized_view_columns"][0]

    # Test materialized view column specific attributes
    assert (
        transformed_data["attributes"]["name"] == expected_column["attributes"]["name"]
    )
    assert (
        transformed_data["attributes"]["dataType"]
        == expected_column["attributes"]["dataType"]
    )
    assert (
        transformed_data["attributes"]["precision"]
        == expected_column["attributes"]["precision"]
    )
    assert (
        transformed_data["attributes"]["numericScale"]
        == expected_column["attributes"]["numericScale"]
    )

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


def test_column_with_custom_attributes(
    transformer: AtlasTransformer,
    raw_data: Dict[str, Any],
    expected_data: Dict[str, Any],
):
    """Test column transformation with custom attributes"""
    workflow_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())

    transformed_data = transformer.transform_metadata(
        "COLUMN", raw_data["columns_with_custom_attrs"][0], workflow_id, run_id
    )

    assert transformed_data is not None
    expected_column = expected_data["columns_with_custom_attrs"][0]

    # Test custom attributes
    assert (
        transformed_data["customAttributes"]["is_self_referencing"]
        == expected_column["customAttributes"]["is_self_referencing"]
    )
    assert (
        transformed_data["customAttributes"]["source_id"]
        == expected_column["customAttributes"]["source_id"]
    )
    assert (
        transformed_data["customAttributes"]["is_auto_increment"]
        == expected_column["customAttributes"]["is_auto_increment"]
    )
    assert (
        transformed_data["customAttributes"]["is_generated"]
        == expected_column["customAttributes"]["is_generated"]
    )

    # Test table relationship
    assert "table" in transformed_data["attributes"]
    assert (
        transformed_data["attributes"]["table"]["uniqueAttributes"]["qualifiedName"]
        == expected_column["attributes"]["table"]["uniqueAttributes"]["qualifiedName"]
    )


def test_column_invalid_data(transformer: AtlasTransformer):
    """Test column transformation with invalid data"""
    workflow_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())

    # Test missing required fields
    invalid_data = {"connection_qualified_name": "default/snowflake/1728518400"}

    transformed_data = transformer.transform_metadata(
        "COLUMN", invalid_data, workflow_id, run_id
    )

    assert transformed_data is None
