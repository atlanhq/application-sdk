import json
import os
from typing import Any, Dict, List

import pytest

from application_sdk.transformers.atlas import AtlasTransformer

procedure_attributes = [
    "qualifiedName",
    "name",
    "tenantId",
    "connectorName",
    "connectionName",
    "connectionQualifiedName",
    "subType",
    "sourceCreatedBy",
    "lastSyncWorkflowName",
    "lastSyncRun",
    "databaseName",
    "databaseQualifiedName",
    "schemaName",
    "schemaQualifiedName",
    "definition",
]


@pytest.fixture
def resources_dir():
    return os.path.join(os.path.dirname(__file__), "resources")


@pytest.fixture
def raw_data(resources_dir: str) -> Dict[str, Any]:
    with open(os.path.join(resources_dir, "raw_procedures.json")) as f:
        return json.load(f)


@pytest.fixture
def expected_data(resources_dir: str) -> Dict[str, Any]:
    with open(os.path.join(resources_dir, "transformed_procedures.json")) as f:
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


def test_simple_procedure_transformation(
    transformer: AtlasTransformer,
    raw_data: Dict[str, Any],
    expected_data: Dict[str, Any],
):
    """Test the transformation of regular table columns"""

    transformed_data = transformer.transform_row(
        "PROCEDURE",
        raw_data["simple_procedure"],
        "test_workflow_id",
        "test_run_id",
        connection_name="test-connection",
        connection_qualified_name="default/postgres/1741637459",
    )

    assert transformed_data is not None
    expected_procedure = expected_data["simple_procedure"]

    assert transformed_data["typeName"] == "Procedure"

    assert_attributes(transformed_data, expected_procedure, procedure_attributes)

    # Test schema relationship
    assert "atlanSchema" in transformed_data["attributes"]
    assert (
        transformed_data["attributes"]["atlanSchema"]["uniqueAttributes"][
            "qualifiedName"
        ]
        == expected_procedure["attributes"]["atlanSchema"]["uniqueAttributes"][
            "qualifiedName"
        ]
    )
