from typing import Any, Dict

import pytest
import requests
from temporalio.client import WorkflowExecutionStatus

from application_sdk.common.logger_adaptors import get_logger
from application_sdk.test_utils.e2e import TestInterface
from application_sdk.test_utils.e2e.conftest import workflow_details

logger = get_logger(__name__)


class WorkflowExecutionError(Exception):
    """Exception class for raising exceptions during workflow execution"""


class BaseTest(TestInterface):
    config_file_path: str
    extracted_output_base_path: str
    schema_base_path: str
    credentials: Dict[str, Any]
    metadata: Dict[str, Any]
    connection: Dict[str, Any]

    @pytest.mark.order(1)
    def test_health_check(self):
        """
        Check if the server is up and running and is responding to requests
        """
        response = requests.get(self.client.host)
        self.assertEqual(response.status_code, 200)

    @pytest.mark.order(2)
    def test_auth(self):
        """
        Test the auth and test connection flow
        """
        response = self.client.test_connection(credentials=self.credentials)
        self.assertEqual(response, self.expected_api_responses["auth"])

    @pytest.mark.order(3)
    def test_metadata(self):
        """
        Test Metadata
        """
        response = self.client.get_metadata(credentials=self.credentials)
        self.assertEqual(response, self.expected_api_responses["metadata"])

    @pytest.mark.order(4)
    def test_preflight_check(self):
        """
        Test Preflight Check
        """
        response = self.client.preflight_check(
            credentials=self.credentials, metadata=self.metadata
        )
        self.assertEqual(response, self.expected_api_responses["preflight_check"])

    @pytest.mark.order(4)
    def test_run_workflow(self):
        """
        Test running the metadata extraction workflow
        """
        response = self.client.run_workflow(
            credentials=self.credentials,
            metadata=self.metadata,
            connection=self.connection,
        )
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

    @pytest.mark.order(5)
    def test_configuration_get(self):
        """
        Test configuration retrieval
        """
        response = requests.get(
            f"{self.client.host}/workflows/v1/config/{workflow_details[self.test_name]['workflow_id']}"
        )
        self.assertEqual(response.status_code, 200)

        response_data = response.json()
        self.assertEqual(response_data["success"], True)
        self.assertEqual(
            response_data["message"], "Workflow configuration fetched successfully"
        )

        # Verify that response data contains the expected metadata and connection
        self.assertEqual(response_data["data"]["connection"], self.connection)
        self.assertEqual(response_data["data"]["metadata"], self.metadata)

    @pytest.mark.order(6)
    def test_configuration_update(self):
        """
        Test configuration update
        """
        update_payload = {
            "connection": self.connection,
            "metadata": {**self.metadata, "temp-table-regex": "^temp_.*"},
        }
        response = requests.post(
            f"{self.client.host}/workflows/v1/config/{workflow_details[self.test_name]['workflow_id']}",
            json=update_payload,
        )
        self.assertEqual(response.status_code, 200)

        response_data = response.json()
        self.assertEqual(response_data["success"], True)
        self.assertEqual(
            response_data["message"], "Workflow configuration updated successfully"
        )
        self.assertEqual(
            response_data["data"]["metadata"]["temp-table-regex"], "^temp_.*"
        )

    @pytest.mark.order(7)
    def test_data_validation(self):
        """
        Test for validating the extracted source data
        """
        self.validate_data()

    @pytest.mark.order(8)
    async def test_scale(self):
        """
        Method to run scale tests using the application workflow.

        This async method should be implemented by subclasses to set up and run the workflow with
        scale test data. The implementation should:

        1. Define an application_sql function that creates and starts the workflow components
        2. Set up mocks/patches to use the test data instead of real database connections
        3. Run the workflow using run_and_monitor_workflow
        4. Validate the results

        Example implementation:

        .. code-block:: python

            async def scale_test(self):
                # Mock implementation for SQL
                mock_sql_input = self.create_mock_sql_input()

                # Set up all necessary patches
                with patch(...), patch(...), ...:
                    # Initialize the temporal client
                    temporal_client = TemporalClient()
                    await temporal_client.load()

                    # Run and monitor the workflow
                    status, time_taken = await run_and_monitor_workflow(
                        application_sql,  # The workflow function to test
                        temporal_client,
                        polling_interval=5,
                        timeout=600,
                    )

                    # Assert workflow completed successfully
                    assert status == "COMPLETED 🟢"
                    assert time_taken > 0

                    return status, time_taken

        Returns:
            Tuple[str, float]: A tuple containing the workflow status and time taken
        """
        if not self.run_scale_test:
            pytest.skip("Scale test is disabled")

        raise NotImplementedError("Subclasses must implement scale_test method")

    @pytest.fixture(scope="class", autouse=True)
    def setup_scale_test_fixture(self):
        if self.run_scale_test:
            self.setup_scale_test_resources()
