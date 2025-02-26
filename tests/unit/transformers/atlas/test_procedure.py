import os

import pytest

from application_sdk.transformers.atlas import AtlasTransformer
from application_sdk.transformers.atlas.sql import Procedure
from application_sdk.transformers.common.utils import build_atlas_qualified_name


@pytest.fixture
def resources_dir() -> str:
    """Return the path to the test resources directory."""
    return os.path.join(os.path.dirname(__file__), "resources")


@pytest.fixture
def transformer() -> AtlasTransformer:
    """Return an instance of AtlasTransformer."""
    return AtlasTransformer(connector_name="snowflake", tenant_id="default")


def test_procedure_parse_obj_valid():
    """Test parsing a valid procedure object."""
    valid_data = {
        "procedure_name": "TEST_PROC",
        "procedure_definition": "CREATE PROCEDURE TEST_PROC() RETURNS STRING LANGUAGE SQL AS 'SELECT 1'",
        "procedure_catalog": "TEST_DB",
        "procedure_schema": "TEST_SCHEMA",
        "connection_qualified_name": "default/snowflake/1728518400",
        "procedure_type": "SQL"
    }

    procedure = Procedure.parse_obj(valid_data)

    # Verify basic attributes
    assert procedure.attributes.name == "TEST_PROC"
    assert procedure.attributes.definition == "CREATE PROCEDURE TEST_PROC() RETURNS STRING LANGUAGE SQL AS 'SELECT 1'"
    assert procedure.attributes.database_name == "TEST_DB"
    assert procedure.attributes.schema_name == "TEST_SCHEMA"
    assert procedure.attributes.connection_qualified_name == "default/snowflake/1728518400"
    assert procedure.sub_type == "SQL"

    # Verify qualified names are built correctly
    expected_schema_qualified_name = build_atlas_qualified_name(
        valid_data["connection_qualified_name"],
        valid_data["procedure_catalog"],
        valid_data["procedure_schema"]
    )
    expected_db_qualified_name = build_atlas_qualified_name(
        valid_data["connection_qualified_name"],
        valid_data["procedure_catalog"]
    )
    assert procedure.attributes.schema_qualified_name == expected_schema_qualified_name
    assert procedure.attributes.database_qualified_name == expected_db_qualified_name


def test_procedure_parse_obj_missing_required_fields():
    """Test parsing a procedure object with missing required fields."""
    invalid_data_cases = [
        ({"procedure_definition": "test", "procedure_catalog": "test", "procedure_schema": "test", "connection_qualified_name": "test"},
         "Procedure name cannot be None"),
        ({"procedure_name": "test", "procedure_catalog": "test", "procedure_schema": "test", "connection_qualified_name": "test"},
         "Procedure definition cannot be None"),
        ({"procedure_name": "test", "procedure_definition": "test", "procedure_schema": "test", "connection_qualified_name": "test"},
         "Procedure catalog cannot be None"),
        ({"procedure_name": "test", "procedure_definition": "test", "procedure_catalog": "test", "connection_qualified_name": "test"},
         "Procedure schema cannot be None"),
        ({"procedure_name": "test", "procedure_definition": "test", "procedure_catalog": "test", "procedure_schema": "test"},
         "Connection qualified name cannot be None"),
    ]

    for invalid_data, expected_error in invalid_data_cases:
        with pytest.raises(ValueError, match=f"Error creating Procedure Entity: {expected_error}"):
            Procedure.parse_obj(invalid_data)


def test_procedure_parse_obj_optional_fields():
    """Test parsing a procedure object with optional fields."""
    data = {
        "procedure_name": "TEST_PROC",
        "procedure_definition": "CREATE PROCEDURE TEST_PROC() RETURNS STRING LANGUAGE SQL AS 'SELECT 1'",
        "procedure_catalog": "TEST_DB",
        "procedure_schema": "TEST_SCHEMA",
        "connection_qualified_name": "default/snowflake/1728518400",
        # Optional field
        "procedure_type": "STORED_PROCEDURE"
    }

    procedure = Procedure.parse_obj(data)
    assert procedure.sub_type == "STORED_PROCEDURE"

    # Test with missing optional field
    data_without_type = {k: v for k, v in data.items() if k != "procedure_type"}
    procedure = Procedure.parse_obj(data_without_type)
    assert procedure.sub_type == "-1"  # Default value


def test_procedure_transformation(transformer: AtlasTransformer):
    """Test the transformation of procedure metadata using AtlasTransformer."""
    procedure_data = {
        "procedure_name": "TEST_PROC",
        "procedure_definition": "CREATE PROCEDURE TEST_PROC() RETURNS STRING LANGUAGE SQL AS 'SELECT 1'",
        "procedure_catalog": "TEST_DB",
        "procedure_schema": "TEST_SCHEMA",
        "connection_qualified_name": "default/snowflake/1728518400",
        "procedure_type": "SQL"
    }

    transformed_data = transformer.transform_metadata(
        "PROCEDURE",
        procedure_data,
        "test_workflow_id",
        "test_run_id",
        connection_name="test-connection",
        connection_qualified_name="default/snowflake/1728518400",
    )

    assert transformed_data is not None
    assert transformed_data["typeName"] == "Procedure"
    assert transformed_data["attributes"]["name"] == "TEST_PROC"
    assert transformed_data["attributes"]["definition"] == "CREATE PROCEDURE TEST_PROC() RETURNS STRING LANGUAGE SQL AS 'SELECT 1'"
    assert transformed_data["attributes"]["databaseName"] == "TEST_DB"
    assert transformed_data["attributes"]["schemaName"] == "TEST_SCHEMA"
    assert transformed_data["attributes"]["connectionQualifiedName"] == "default/snowflake/1728518400"
    assert transformed_data["attributes"]["subType"] == "SQL"
