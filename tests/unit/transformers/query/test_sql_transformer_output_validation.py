import glob
import json
import os
from typing import Any, Dict, List

import daft
import pytest

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.transformers.query import QueryBasedTransformer

logger = get_logger(__name__)

LAST_SYNC_WORKFLOW_NAME = "79a40801-07c2-4852-86c4-9703bda3a840"
LAST_SYNC_RUN = "019667f9-31e9-77b0-b7c0-b901bd30d140"
CONNECTOR_NAME = "postgres"
TENANT_ID = "default"
CONNECTION_QUALIFIED_NAME = "default/postgres/1745501106"
CONNECTION_NAME = "dev"


@pytest.fixture
def sql_transformer():
    return QueryBasedTransformer(connector_name=CONNECTOR_NAME, tenant_id=TENANT_ID)


def get_raw_json_files():
    """
    Get all JSON files from the resources/raw directory using glob
    """
    current_dir = os.path.dirname(os.path.abspath(__file__))
    resources_dir = os.path.join(current_dir, "resources/raw")
    return glob.glob(os.path.join(resources_dir, "*.json"))


def remove_run_time_sensitive_fields(row: Dict[str, Any]):
    """
    Remove run time sensitive fields from a row
    E.g time sensitive fields: lastSyncRunAt, createdAt, updatedAt which can change on each run
    """
    row["attributes"].pop("lastSyncRunAt")


def test_transform_metadata_output_validation(sql_transformer):
    """
    Test the complete transformation flow for all JSON files in resources:
    1. Read raw JSON from resources using daft.read_json
    2. Transform using SQL transformer
    3. Validate output
    """
    test_files = get_raw_json_files()
    assert len(test_files) > 0, "No test files found in resources directory"
    logger.info("Found %d test files to process", len(test_files))

    for json_file in test_files:
        file_name = os.path.basename(json_file).removesuffix(".json").upper()
        logger.info("Testing for Asset: %s", file_name)

        # Read the json file into a Daft DataFrame
        input_df = daft.read_json(json_file)

        # Transform using SQL transformer
        result = sql_transformer.transform_metadata(
            file_name,
            input_df,
            LAST_SYNC_WORKFLOW_NAME,
            LAST_SYNC_RUN,
            connection_qualified_name=CONNECTION_QUALIFIED_NAME,
            connection_name=CONNECTION_NAME,
        )

        # Assert that the result is not None and has rows
        assert result is not None
        assert result.count_rows() > 0

        # convert the transformed Daft DataFrame to a list of records
        transformed_result_ouput = result.to_pylist()

        # read the expected transformed json file
        expected_transformed_output: List[Dict[str, Any]] = []
        with open(json_file.replace("raw", "transformed"), "r") as f:
            for line in f:
                json_object = json.loads(line)
                expected_transformed_output.append(json_object)

        # assert that the number of records in the transformed output is the same as the expected output
        assert len(transformed_result_ouput) == len(expected_transformed_output)
        logger.info(
            "Validating %d records for %s", len(transformed_result_ouput), file_name
        )

        # validate each record in the transformed output with the expected output
        for idx, (expected, actual) in enumerate(
            zip(expected_transformed_output, transformed_result_ouput)
        ):
            logger.info("Validating record %d of %s", idx + 1, file_name)
            remove_run_time_sensitive_fields(expected)
            remove_run_time_sensitive_fields(actual)
            assert expected == actual
            logger.info("Record %d validation successful", idx + 1)

        logger.info("All records validated successfully for %s", file_name)
