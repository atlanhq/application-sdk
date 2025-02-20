import inspect
import os
from abc import abstractmethod
from glob import glob
from typing import Any, Dict

import orjson
import pandas as pd
import pandera.extensions as extensions
from pandera.io import from_yaml

from application_sdk.common.logger_adaptors import get_logger
from application_sdk.test_utils.e2e.client import FastApiServerClient
from application_sdk.test_utils.e2e.conftest import WorkflowDetails
from application_sdk.test_utils.e2e.utils import load_config_from_yaml

logger = get_logger(__name__)


# Custom Tests
@extensions.register_check_method(statistics=["expected_record_count"])
def check_record_count_ge(df: pd.DataFrame, *, expected_record_count) -> bool:
    if df.shape[0] >= expected_record_count:
        return True
    else:
        raise ValueError(
            f"Expected record count: {expected_record_count}, got: {df.shape[0]}"
        )


class TestInterface:
    """
    Interface for end-to-end tests.
    Attributes:
        config_file_path (str): Path to the configuration file.
        extracted_output_base_path (str): Base path for extracted output.
        expected_output_base_path (str): Base path for expected output.
        credentials (Dict[str, Any]): Credentials for the test.
        metadata (Dict[str, Any]): Metadata for the test.
        connection (Dict[str, Any]): Connection details for the test.
        workflow_timeout (int): Timeout for the workflow, default is 300 seconds.
    Methods:
        setup_class(cls):
            Sets up the class by preparing directory paths and loading configuration.
        test_health_check():
            Abstract method for health check test.
        test_auth():
            Abstract method for authentication test.
        metadata():
            Abstract method for metadata test.
        preflight_check():
            Abstract method for preflight check test.
        run_workflow():
            Abstract method for running the workflow.
        prepare_dir_paths(cls):
            Prepares directory paths for the test.
        validate_data():
            Validates the data against the schema.
    """

    config_file_path: str
    extracted_output_base_path: str
    expected_output_base_path: str
    credentials: Dict[str, Any]
    metadata: Dict[str, Any]
    connection: Dict[str, Any]
    workflow_timeout: int = 300

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
        cls.client = FastApiServerClient(
            host=config["server_config"]["server_host"],
            version=config["server_config"]["server_version"],
        )

    @abstractmethod
    def test_health_check(self):
        """
        Method to test the health check of the server.
        """
        raise NotImplementedError

    @abstractmethod
    def test_auth(self):
        """
        Method to test the test authentication.
        """
        raise NotImplementedError

    @abstractmethod
    def test_metadata(self):
        """
        Method to test the metadata
        """
        raise NotImplementedError

    @abstractmethod
    def test_preflight_check(self):
        """
        Method to test the preflight check
        """
        raise NotImplementedError

    @abstractmethod
    def test_run_workflow(self):
        """
        Method to run the workflow
        """
        raise NotImplementedError

    @classmethod
    def prepare_dir_paths(cls):
        """
        Prepares directory paths for the test to pick up the configuration and schema files.
        """
        tests_dir = os.path.dirname(inspect.getfile(cls))
        cls.config_file_path = f"{tests_dir}/config.yaml"
        if not os.path.exists(cls.config_file_path):
            raise FileNotFoundError(f"Config file not found: {cls.config_file_path}")
        cls.schema_base_path = f"{tests_dir}/schema"
        if not os.path.exists(cls.schema_base_path):
            raise FileNotFoundError(
                f"Schema base path not found: {cls.schema_base_path}"
            )
        cls.extracted_output_base_path = "/tmp/output"

    def validate_data(self):
        """
        Method to validate the data against the schema.
        It picks up the schema files from the schema directory and validates the data against it.
        """
        logger.info("Starting data validation tests")
        schema_file_search_string = f"{self.schema_base_path}/**/*"

        # Perform a recursive search for all the schema files in yaml/yml format
        yaml_file_list = glob(
            f"{schema_file_search_string}.yaml", recursive=True
        ) + glob(f"{schema_file_search_string}.yml", recursive=True)
        for schema_yaml_file_path in yaml_file_list:
            expected_file_postfix = (
                schema_yaml_file_path.replace(self.schema_base_path, "")
                .replace(".yaml", "")
                .replace(".yml", "")
            )
            logger.info(f"Validating data for: {expected_file_postfix}")

            # Load the pandera schema from the yaml file
            schema = from_yaml(schema_yaml_file_path)
            extracted_dir_path = f"{self.extracted_output_base_path}/{WorkflowDetails.workflow_id}/{WorkflowDetails.run_id}{expected_file_postfix}"
            data = []
            for f_name in glob(f"{extracted_dir_path}/*.json"):
                with open(f_name, "rb") as f:
                    data.extend([orjson.loads(line) for line in f])

            # We can have nested data in the json files, so we need to flatten it
            dataframe = pd.json_normalize(data)
            schema.validate(dataframe, lazy=True)
            logger.info(f"Data Validation for {expected_file_postfix} successful")
