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

def test_procedure_transformation(transformer: AtlasTransformer):
    """Test the transformation of procedure metadata using AtlasTransformer."""
    procedure_data = {
        "procedure_name": "TEST_PROC",
        "procedure_definition": "CREATE PROCEDURE TEST_PROC() RETURNS STRING LANGUAGE SQL AS 'SELECT 1'",
        "procedure_catalog": "TEST_DB",
        "procedure_schema": "TEST_SCHEMA",
        "connection_qualified_name": "default/snowflake/1728518400",
        "procedure_type": "SQL",
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
    assert (
        transformed_data["attributes"]["definition"]
        == "CREATE PROCEDURE TEST_PROC() RETURNS STRING LANGUAGE SQL AS 'SELECT 1'"
    )
    assert transformed_data["attributes"]["databaseName"] == "TEST_DB"
    assert transformed_data["attributes"]["schemaName"] == "TEST_SCHEMA"
    assert (
        transformed_data["attributes"]["connectionQualifiedName"]
        == "default/snowflake/1728518400"
    )
    assert transformed_data["attributes"]["subType"] == "SQL"
