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

from application_sdk.observability.logger_adaptor import get_logger
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


class WorkflowExecutionError(Exception):
    """Exception class for raising exceptions during workflow execution"""


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
    expected_dir_path: Optional[str] = None
    test_workflow_args: Dict[str, Any]
    workflow_timeout: Optional[int] = 200
    polling_interval: int = 10

    @classmethod
    def setup_class(cls):
        """
        Sets up the class by preparing directory paths and loading configuration.
        """
        cls.prepare_dir_paths()

        # Load configuration
        cls.config = load_config_from_yaml(yaml_file_path=cls.config_file_path)

        # Set common configuration
        cls.expected_api_responses = cls.config.get("expected_api_responses", {})
        cls.test_workflow_args = cls.config.get("test_workflow_args", {})
        cls.test_name = cls.config["test_name"]

        # Set up API client
        cls.client = APIServerClient(
            host=cls.config["server_config"]["server_host"],
            version=cls.config["server_config"]["server_version"],
        )

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
    def test_run_workflow(self):
        """
        Test running the metadata extraction workflow
        """
        raise NotImplementedError

    def run_workflow(self):
        """
        Test running the metadata extraction workflow
        """
        response = self.client.run_workflow(data=self.test_workflow_args)
        self.assertEqual(response["success"], True)
        self.assertEqual(response["message"], "Workflow started successfully")
        workflow_details[self.test_name] = {
            "workflow_id": response["data"]["workflow_id"],
            "run_id": response["data"]["run_id"],
        }

        # Wait for the workflow to complete
        workflow_status = self.monitor_and_wait_workflow_execution()

        # If worklfow is not completed successfully, raise an exception
        if workflow_status != WorkflowExecutionStatus.COMPLETED.name:
            raise WorkflowExecutionError(
                f"Workflow failed with status: {workflow_status}"
            )

        logger.info("Workflow completed successfully")

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

    @abstractmethod
    def _get_extracted_dir_path(self, expected_file_postfix: str) -> str:
        """
        Method to get the extracted directory path

        Args:
            expected_file_postfix (str): Postfix for the expected file
        Returns:
            str: Extracted directory path
        """
        raise NotImplementedError

    def _get_normalised_dataframe(self, extracted_file_path: str) -> "pd.DataFrame":
        """
        Method to get the normalised dataframe of the extracted data

        Args:
            expected_file_postfix (str): Postfix for the expected file
        Returns:
            pd.DataFrame: Normalised dataframe of the extracted data
        """
        data = []

        # Check if there are json or parquet files in the extracted directory
        files_list = glob(f"{extracted_file_path}/**/*.json", recursive=True) or glob(
            f"{extracted_file_path}/**/*.parquet", recursive=True
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
                f"No data found in the extracted directory: {extracted_file_path}"
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

            extracted_file_path = self._get_extracted_dir_path(expected_file_postfix)

            logger.info(f"Validating data for: {expected_file_postfix}")
            # Load the pandera schema from the yaml file
            schema = from_yaml(schema_yaml_file_path)
            dataframe = self._get_normalised_dataframe(extracted_file_path)
            schema.validate(dataframe, lazy=True)
            logger.info(f"Data Validation for {expected_file_postfix} successful")
