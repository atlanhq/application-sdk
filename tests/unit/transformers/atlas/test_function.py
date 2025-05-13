import json
import os
from typing import Any, Dict

import pytest
from pyatlan.model.assets import Function

from application_sdk.transformers.atlas import AtlasTransformer


@pytest.fixture
def resources_dir() -> str:
    return os.path.join(os.path.dirname(__file__), "resources")


@pytest.fixture
def raw_data(resources_dir: str) -> Dict[str, Any]:
    with open(os.path.join(resources_dir, "raw_functions.json")) as f:
        return json.load(f)


@pytest.fixture
def expected_data(resources_dir: str) -> Dict[str, Any]:
    with open(os.path.join(resources_dir, "transformed_functions.json")) as f:
        return json.load(f)


@pytest.fixture
def transformer() -> AtlasTransformer:
    return AtlasTransformer(connector_name="snowflake", tenant_id="default")


def assert_attributes(
    transformed_data: Dict[str, Any],
    expected_data: Dict[str, Any],
    attributes: list[str],
    is_custom: bool = False,
) -> None:
    """Helper function to assert attribute values match expected values."""
    attr_type = "customAttributes" if is_custom else "attributes"
    for attr in attributes:
        assert (
            transformed_data[attr_type][attr] == expected_data[attr_type][attr]
        ), f"Mismatch in {'custom ' if is_custom else ''}{attr}"


def test_function_initialization():
    """Test Function initialization with basic attributes."""
    # Create function with basic attributes
    function = Function()
    function.attributes.name = "test_function"
    function.attributes.qualified_name = (
        "default/snowflake/1728518400/TEST_DB/TEST_SCHEMA/test_function"
    )
    function.attributes.schema_qualified_name = (
        "default/snowflake/1728518400/TEST_DB/TEST_SCHEMA"
    )
    function.attributes.schema_name = "TEST_SCHEMA"
    function.attributes.database_name = "TEST_DB"
    function.attributes.database_qualified_name = "default/snowflake/1728518400/TEST_DB"
    function.attributes.connection_qualified_name = "default/snowflake/1728518400"

    # Verify attributes are set correctly
    assert function.attributes.name == "test_function"
    assert function.attributes.schema_name == "TEST_SCHEMA"
    assert function.attributes.database_name == "TEST_DB"
    assert (
        function.attributes.database_qualified_name
        == "default/snowflake/1728518400/TEST_DB"
    )
    assert (
        function.attributes.connection_qualified_name == "default/snowflake/1728518400"
    )
    assert (
        function.attributes.qualified_name
        == "default/snowflake/1728518400/TEST_DB/TEST_SCHEMA/test_function"
    )


def test_function_invalid_data(transformer: AtlasTransformer):
    """Test function transformation with invalid data."""
    # Test missing required fields
    invalid_data = {
        "connection_qualified_name": "default/snowflake/1728518400",
    }

    transformed_data = transformer.transform_row(
        "FUNCTION",
        invalid_data,
        "test_workflow_id",
        "test_run_id",
        connection_name="test-connection",
        connection_qualified_name="default/snowflake/1728518400",
    )

    assert transformed_data is None


def test_function_from_dict():
    """Test creating Function from dictionary data."""
    # Test with valid data
    valid_data = {
        "function_name": "test_function",
        "argument_signature": "(arg1 type1)",
        "function_definition": "SELECT 1",
        "is_external": "NO",
        "is_memoizable": "YES",
        "function_language": "SQL",
        "function_catalog": "TEST_DB",
        "function_schema": "TEST_SCHEMA",
        "connection_qualified_name": "default/snowflake/1728518400",
        "data_type": "NUMBER",
        "function_type": "Scalar",
    }

    function = Function()
    function.attributes.name = valid_data["function_name"]
    function.attributes.function_definition = valid_data["function_definition"]
    function.attributes.function_language = valid_data["function_language"]
    function.attributes.function_is_external = valid_data["is_external"] == "NO"
    function.attributes.function_is_memoizable = valid_data["is_memoizable"] == "YES"
    function.attributes.function_return_type = valid_data["data_type"]
    function.attributes.function_type = valid_data["function_type"]
    function.attributes.function_arguments = {
        valid_data["argument_signature"].strip("()")
    }
    function.attributes.database_name = valid_data["function_catalog"]
    function.attributes.schema_name = valid_data["function_schema"]
    function.attributes.connection_qualified_name = valid_data[
        "connection_qualified_name"
    ]

    # Verify the function attributes
    assert function.attributes.name == "test_function"
    assert function.attributes.function_definition == "SELECT 1"
    assert function.attributes.function_language == "SQL"
    assert function.attributes.function_is_external is True
    assert function.attributes.function_is_memoizable is True
    assert function.attributes.function_return_type == "NUMBER"
    assert function.attributes.function_type == "Scalar"
    assert function.attributes.function_arguments == {"arg1 type1"}
    assert function.attributes.database_name == "TEST_DB"
    assert function.attributes.schema_name == "TEST_SCHEMA"
    assert (
        function.attributes.connection_qualified_name == "default/snowflake/1728518400"
    )

    # Test with invalid data (missing required fields)
    invalid_data = {"connection_qualified_name": "default/snowflake/1728518400"}

    function = Function()
    function.attributes.connection_qualified_name = invalid_data[
        "connection_qualified_name"
    ]
    assert function.attributes.name is None
    assert function.attributes.function_definition is None
