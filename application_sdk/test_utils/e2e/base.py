from typing import Any, Dict

import pytest
import requests
from temporalio.client import WorkflowExecutionStatus

from application_sdk.common.logger_adaptors import get_logger
from application_sdk.test_utils.e2e import TestInterface
from application_sdk.test_utils.e2e.conftest import WorkflowDetails

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
        WorkflowDetails.workflow_id = response["data"]["workflow_id"]
        WorkflowDetails.run_id = response["data"]["run_id"]

        # Wait for the workflow to complete
        workflow_status = self.monitor_and_wait_workflow_execution()

        # If worklfow is not completed successfully, raise an exception
        if workflow_status != WorkflowExecutionStatus.COMPLETED.name:
            raise WorkflowExecutionError(
                f"Workflow failed with status: {workflow_status}"
            )

        logger.info("Workflow completed successfully")

    @pytest.mark.order(5)
    def test_configuration_update(self):
        """
        Test configuration update
        """
        update_payload = {
            "connection": self.connection,
            "metadata": {**self.metadata, "temp-table-regex": "^temp_.*"},
        }
        response = requests.post(
            f"{self.client.host}/workflows/v1/config/{WorkflowDetails.workflow_id}",
            json=update_payload,
        )
        self.assertEqual(response.status_code, 200)
        response_data = response.json()
        self.assertEqual(response_data, update_payload)

    @pytest.mark.order(6)
    def test_configuration_get(self):
        """
        Test configuration retrieval
        """
        update_payload = {
            "connection": self.connection,
            "metadata": {**self.metadata, "temp-table-regex": "^temp_.*"},
        }
        response = requests.get(
            f"{self.client.host}/workflows/v1/config/{WorkflowDetails.workflow_id}"
        )
        self.assertEqual(response.status_code, 200)
        response_data = response.json()
        self.assertEqual(response_data, update_payload)

    @pytest.mark.order(7)
    def test_data_validation(self):
        """
        Test for validating the extracted source data
        """
        self.validate_data()
