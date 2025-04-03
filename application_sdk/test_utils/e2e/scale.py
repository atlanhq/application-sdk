from typing import Any, Dict

import pytest
import requests
from temporalio.client import WorkflowExecutionStatus

from application_sdk.common.logger_adaptors import get_logger
from application_sdk.test_utils.e2e import TestInterface

logger = get_logger(__name__)


class WorkflowExecutionError(Exception):
    """Exception class for raising exceptions during workflow execution"""


class ScaleTest(TestInterface):
    config_file_path: str
    extracted_output_base_path: str
    credentials: Dict[str, Any]
    metadata: Dict[str, Any]
    connection: Dict[str, Any]
    connection_url: str

    @pytest.mark.order(1)
    def test_health_check(self):
        """
        Check if the server is up and running and is responding to requests
        """
        response = requests.get(self.client.host)
        self.assertEqual(response.status_code, 200)

    @pytest.mark.order(2)
    def test_scale(self):
        """
        Method to run scale tests using the application workflow.

        This method handles different test types:
        - source_data_generator: Generates data in source database, runs workflow, and cleans up
        - testcontainers: Spawns test container, generates data, runs workflow, and cleans up
        - Other test types should be implemented by subclasses
        """
        if self.test_type not in ["source_data_generator", "testcontainers", "duckdb"]:
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
                self.setup_scale_test_resources_source_data_generator(
                    self.connection_url
                )

                # Run the workflow
                logger.info("Running workflow...")
                workflow_status = self.client.run_workflow(
                    credentials=self.credentials,
                    metadata=self.metadata,
                    connection=self.connection,
                )

                # Store workflow details
                self.workflow_details = {
                    "workflow_id": workflow_status["data"]["workflow_id"],
                    "run_id": workflow_status["data"]["run_id"],
                }

                # Assert workflow started successfully
                self.assertEqual(workflow_status["success"], True)
                self.assertEqual(
                    workflow_status["message"], "Workflow started successfully"
                )
                self.assertIn("workflow_id", workflow_status["data"])
                self.assertIn("run_id", workflow_status["data"])

                # Wait for the workflow to complete
                workflow_status = self.monitor_and_wait_workflow_execution()

                if workflow_status != WorkflowExecutionStatus.COMPLETED.name:
                    raise WorkflowExecutionError(
                        f"Workflow failed with status: {workflow_status}"
                    )

                logger.info("Workflow completed successfully")

                # Clean up generated data
                logger.info("Cleaning up generated data...")
                self.teardown_scale_test_resources_source_data_generator(
                    self.connection_url
                )

            except Exception as e:
                logger.error(f"Error during source data generator test: {e}")
                raise WorkflowExecutionError(f"Source data generator test failed: {e}")

        elif self.test_type == "testcontainers":
            try:
                # Set up test container and generate data
                logger.info("Setting up test container and generating data...")
                self.setup_scale_test_resources_testcontainers()

                # Run the workflow
                logger.info("Running workflow...")
                workflow_status = self.client.run_workflow(
                    credentials=self.credentials,
                    metadata=self.metadata,
                    connection=self.connection,
                )

                # Store workflow details
                self.workflow_details = {
                    "workflow_id": workflow_status["data"]["workflow_id"],
                    "run_id": workflow_status["data"]["run_id"],
                }

                # Assert workflow started successfully
                self.assertEqual(workflow_status["success"], True)
                self.assertEqual(
                    workflow_status["message"], "Workflow started successfully"
                )
                self.assertIn("workflow_id", workflow_status["data"])
                self.assertIn("run_id", workflow_status["data"])

                # Wait for the workflow to complete
                workflow_status = self.monitor_and_wait_workflow_execution()

                if workflow_status != WorkflowExecutionStatus.COMPLETED.name:
                    raise WorkflowExecutionError(
                        f"Workflow failed with status: {workflow_status}"
                    )

                logger.info("Workflow completed successfully")

            except Exception as e:
                logger.error(f"Error during testcontainers test: {e}")
                raise WorkflowExecutionError(f"Testcontainers test failed: {e}")

    @pytest.fixture(scope="class", autouse=True)
    def setup_scale_test_duckdb_fixture(self):
        if self.test_type == "duckdb":
            self.setup_scale_test_resources_duckdb()
