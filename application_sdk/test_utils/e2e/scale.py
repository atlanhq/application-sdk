from typing import Any, Dict, Tuple

import pytest
import requests

from application_sdk.common.logger_adaptors import get_logger
from application_sdk.test_utils.e2e import TestInterface
from application_sdk.test_utils.scale_data_generator.test_containers.driver import (
    DriverArgs as TestcontainersDriverArgs,
)
from application_sdk.test_utils.scale_data_generator.test_containers.driver import (
    driver as testcontainers_driver,
)
from application_sdk.test_utils.scale_data_generator.test_on_source.driver import (
    DriverArgs as SourceDriverArgs,
)
from application_sdk.test_utils.scale_data_generator.test_on_source.driver import (
    driver as source_driver,
)

logger = get_logger(__name__)


class WorkflowExecutionError(Exception):
    """Exception class for raising exceptions during workflow execution"""


class ScaleTest(TestInterface):
    config_file_path: str
    extracted_output_base_path: str
    schema_base_path: str
    credentials: Dict[str, Any]
    metadata: Dict[str, Any]
    connection: Dict[str, Any]
    run_scale_test: bool = True

    @pytest.mark.order(1)
    def test_health_check(self):
        """
        Check if the server is up and running and is responding to requests
        """
        response = requests.get(self.client.host)
        self.assertEqual(response.status_code, 200)

    @pytest.mark.order(2)
    def test_scale(self) -> Tuple[str, float]:
        """
        Method to run scale tests using the application workflow.

        This method handles different test types:
        - source_data_generator: Generates data in source database, runs workflow, and cleans up
        - testcontainers: Spawns test container, generates data, runs workflow, and cleans up
        - Other test types should be implemented by subclasses

        Returns:
            Tuple[str, float]: A tuple containing the workflow status and time taken
        """
        if not self.run_scale_test:
            pytest.skip("Scale test is disabled")

        if self.test_type == "source_data_generator":
            try:
                # Validate required credentials
                required_creds = ["username", "password", "host", "port"]
                for cred in required_creds:
                    if not self.credentials.get(cred):
                        raise ValueError(f"Missing required credential: {cred}")

                # Generate test data in source database
                logger.info("Generating test data in source database...")
                source_driver(
                    SourceDriverArgs(
                        config_path=self.scale_test_config_path,
                        db_name=self.credentials.get("database", "postgres"),
                        source_type=self.app_type,
                        username=str(self.credentials["username"]),
                        password=str(self.credentials["password"]),
                        host=str(self.credentials["host"]),
                        port=str(self.credentials["port"]),
                    )
                )

                # Run the workflow
                logger.info("Running workflow...")
                workflow_status = self.test_run_workflow()

                # Assert workflow completed successfully
                assert (
                    workflow_status == "COMPLETED"
                ), f"Workflow failed with status: {workflow_status}"

                # Clean up generated data
                logger.info("Cleaning up generated data...")
                source_driver(
                    SourceDriverArgs(
                        config_path=self.scale_test_config_path,
                        db_name=self.credentials.get("database", "postgres"),
                        source_type=self.app_type,
                        username=str(self.credentials["username"]),
                        password=str(self.credentials["password"]),
                        host=str(self.credentials["host"]),
                        port=str(self.credentials["port"]),
                        clean=True,
                    )
                )

                return (
                    workflow_status,
                    0.0,
                )  # Time taken is not relevant for this test type

            except Exception as e:
                logger.error(f"Error during source data generator test: {e}")
                raise WorkflowExecutionError(f"Source data generator test failed: {e}")

        elif self.test_type == "testcontainers":
            try:
                # Set up test container and generate data
                logger.info("Setting up test container and generating data...")
                testcontainers_driver(
                    TestcontainersDriverArgs(
                        config_path=self.scale_test_config_path,
                        source_type=self.app_type,
                        container_class=self.get_container_class(),
                    )
                )

                # Run the workflow
                logger.info("Running workflow...")
                workflow_status = self.test_run_workflow()

                # Assert workflow completed successfully
                assert (
                    workflow_status == "COMPLETED"
                ), f"Workflow failed with status: {workflow_status}"

                # Note: The test container is automatically cleaned up by the driver
                # in its finally block, so no explicit cleanup is needed here

                return (
                    workflow_status,
                    0.0,
                )  # Time taken is not relevant for this test type

            except Exception as e:
                logger.error(f"Error during testcontainers test: {e}")
                raise WorkflowExecutionError(f"Testcontainers test failed: {e}")

        # For other test types, let subclasses implement their own logic
        raise NotImplementedError(
            "Subclasses must implement scale_test method for other test types"
        )

    @pytest.fixture(scope="class", autouse=True)
    def setup_scale_test_fixture(self):
        if self.run_scale_test:
            self.setup_scale_test_resources()
