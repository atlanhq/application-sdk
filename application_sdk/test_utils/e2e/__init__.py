import inspect
import os
import time
from abc import abstractmethod
from glob import glob
from typing import Any, Dict, List, Optional

import orjson
import pandas as pd
import pandera.extensions as extensions
from pandera.io import from_yaml
from temporalio.client import WorkflowExecutionStatus

from application_sdk.common.logger_adaptors import get_logger
from application_sdk.test_utils.e2e.client import APIServerClient
from application_sdk.test_utils.e2e.conftest import workflow_details
from application_sdk.test_utils.e2e.utils import load_config_from_yaml

logger = get_logger(__name__)


# Custom Tests
@extensions.register_check_method(statistics=["expected_record_count"])
def check_record_count_ge(df: pd.DataFrame, *, expected_record_count) -> bool:
    if df.shape[0] >= expected_record_count:
        return True
    else:
        raise ValueError(
            f"Expected record count should be greater than or equal to {expected_record_count}, got: {df.shape[0]}"
        )


class TestInterface:
    """Interface for end-to-end tests.

    This class provides an interface for running end-to-end tests, including methods for
    health checks, authentication, metadata validation, and workflow execution.

    Attributes:
        config_file_path: Path to the configuration file.
        extracted_output_base_path: Base path for extracted output.
        expected_output_base_path: Base path for expected output.
        credentials: Credentials dictionary for the test.
        metadata: Metadata dictionary for the test.
        connection: Connection details dictionary for the test.
        workflow_timeout: Timeout in seconds for the workflow. Defaults to 300.
        polling_interval: Interval in seconds between polling attempts. Defaults to 10.
    """

    config_file_path: str
    extracted_output_base_path: str
    expected_output_base_path: str
    credentials: Dict[str, Any]
    metadata: Dict[str, Any]
    connection: Dict[str, Any]
    workflow_timeout: Optional[int] = 200
    polling_interval: int = 10

    @classmethod
    def setup_class(cls):
        """
        Sets up the class by preparing directory paths and loading configuration.
        """
        cls.prepare_dir_paths()
        config = load_config_from_yaml(yaml_file_path=cls.config_file_path)
        cls.expected_api_responses = config["expected_api_responses"]
        cls.credentials = config["credentials"]
        cls.metadata = config["metadata"]
        cls.connection = config["connection"]
        cls.client = APIServerClient(
            host=config["server_config"]["server_host"],
            version=config["server_config"]["server_version"],
        )
        cls.test_name = config["test_name"]

    @abstractmethod
    def test_health_check(self):
        """Test the health check endpoint of the server.

        This method should verify that the server's health check endpoint
        is responding correctly and the service is operational.

        Raises:
            NotImplementedError: If the subclass does not implement this method.
            AssertionError: If the health check fails.
        """
        raise NotImplementedError

    @abstractmethod
    def test_auth(self):
        """Test the authentication functionality.

        This method should verify that the authentication process works correctly,
        including token generation, validation, and error handling for invalid
        credentials.

        Raises:
            NotImplementedError: If the subclass does not implement this method.
            AssertionError: If authentication tests fail.
        """
        raise NotImplementedError

    @abstractmethod
    def test_metadata(self):
        """Test metadata validation and processing.

        This method should verify that metadata is correctly validated, processed,
        and stored according to the application's requirements. It should test
        both valid and invalid metadata scenarios.

        Raises:
            NotImplementedError: If the subclass does not implement this method.
            AssertionError: If metadata validation tests fail.
        """
        raise NotImplementedError

    @abstractmethod
    def test_preflight_check(self):
        """Test the preflight check functionality.

        This method should verify that the preflight check process correctly
        validates all prerequisites before workflow execution, including
        permissions, resource availability, and configuration.

        Raises:
            NotImplementedError: If the subclass does not implement this method.
            AssertionError: If preflight check tests fail.
        """
        raise NotImplementedError

    @abstractmethod
    def test_run_workflow(self):
        """Test the workflow execution process.

        This method should verify the complete workflow execution process,
        including initialization, task execution, error handling, and result
        validation. It should test both successful and error scenarios.

        Raises:
            NotImplementedError: If the subclass does not implement this method.
            AssertionError: If workflow execution tests fail.
        """
        raise NotImplementedError

    @classmethod
    def prepare_dir_paths(cls):
        """
        Prepares directory paths for the test to pick up the configuration and schema files.
        """
        # Prepare the base directory path
        tests_dir = os.path.dirname(inspect.getfile(cls))

        # Prepare the config file path
        cls.config_file_path = f"{tests_dir}/config.yaml"
        if not os.path.exists(cls.config_file_path):
            raise FileNotFoundError(f"Config file not found: {cls.config_file_path}")

        # Prepare the schema files base path
        cls.schema_base_path = f"{tests_dir}/schema"
        if not os.path.exists(cls.schema_base_path):
            raise FileNotFoundError(
                f"Schema base path not found: {cls.schema_base_path}"
            )

        # Prepare the extracted output base path
        cls.extracted_output_base_path = "/tmp/output"

    def monitor_and_wait_workflow_execution(self) -> str:
        """
        Method to monitor the workflow execution
        by polling the workflow status until the workflow is completed.

        Returns:
            str: Status of the workflow
        """
        # Wait for the workflow to complete
        start_time = time.time()
        while True:
            # Get the workflow status using the API
            workflow_status_response = self.client.get_workflow_status(
                workflow_details[self.test_name]["workflow_id"],
                workflow_details[self.test_name]["run_id"],
            )

            self.run_id = workflow_status_response["data"]["last_executed_run_id"]

            # Get the actual status from the response
            self.assertEqual(workflow_status_response["success"], True)
            current_status = workflow_status_response["data"]["status"]

            # Validate the status and break the loop if the workflow is completed
            if current_status != WorkflowExecutionStatus.RUNNING.name:
                # if the workflow is not RUNNING
                # break the loop and return the status of the workflow
                return current_status

            # Check if the workflow is running beyond the expected time and raise a timeout error
            if (
                self.workflow_timeout
                and (time.time() - start_time) > self.workflow_timeout
            ):
                raise TimeoutError("Workflow did not complete in the expected time")

            # Wait for the polling interval before checking the status again
            time.sleep(self.polling_interval)

    def _get_normalised_dataframe(self, expected_file_postfix: str) -> "pd.DataFrame":
        """
        Method to get the normalised dataframe of the extracted data

        Args:
            expected_file_postfix (str): Postfix for the expected file
        Returns:
            pd.DataFrame: Normalised dataframe of the extracted data
        """
        extracted_dir_path = f"{self.extracted_output_base_path}/{workflow_details[self.test_name]['workflow_id']}/{workflow_details[self.test_name]['run_id']}{expected_file_postfix}"
        data = []

        # Check if there are json or parquet files in the extracted directory
        files_list = glob(f"{extracted_dir_path}/*.json") or glob(
            f"{extracted_dir_path}/*.parquet"
        )
        for f_name in files_list or []:
            if f_name.endswith(".parquet"):
                df = pd.read_parquet(f_name)
                data.extend(df.to_dict(orient="records"))
            if f_name.endswith(".json"):
                with open(f_name, "rb") as f:
                    data.extend([orjson.loads(line) for line in f])

        if not data:
            raise FileNotFoundError(
                f"No data found in the extracted directory: {extracted_dir_path}"
            )
        return pd.json_normalize(data)

    def _get_all_schema_file_paths(self) -> List[str]:
        """
        Method to get all the schema file paths

        Returns:
            List[str]: List of schema file paths
        """
        schema_file_search_string = f"{self.schema_base_path}/**/*"

        # Perform a recursive search for all the schema files in yaml/yml format
        yaml_file_list = glob(
            f"{schema_file_search_string}.yaml", recursive=True
        ) + glob(f"{schema_file_search_string}.yml", recursive=True)

        if not yaml_file_list:
            raise FileNotFoundError(
                f"No schema files found in the schema base path: {self.schema_base_path}"
            )
        return yaml_file_list

    def validate_data(self):
        """
        Method to validate the data against the schema.
        It picks up the schema files from the schema directory and validates the data against it.
        """
        logger.info("Starting data validation tests")

        yaml_files = self._get_all_schema_file_paths()
        for schema_yaml_file_path in yaml_files:
            expected_file_postfix = (
                schema_yaml_file_path.replace(self.schema_base_path, "")
                .replace(".yaml", "")
                .replace(".yml", "")
            )

            logger.info(f"Validating data for: {expected_file_postfix}")
            # Load the pandera schema from the yaml file
            schema = from_yaml(schema_yaml_file_path)
            dataframe = self._get_normalised_dataframe(expected_file_postfix)
            schema.validate(dataframe, lazy=True)
            logger.info(f"Data Validation for {expected_file_postfix} successful")
