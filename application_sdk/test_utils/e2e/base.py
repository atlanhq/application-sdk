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

    @pytest.mark.order(5)
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

    @pytest.mark.order(6)
    def test_data_validation(self):
        """
        Test for validating the extracted source data
        """
        self.validate_data()

    @pytest.mark.order(7)
    def test_health_check_negative(self):
        """
        Test health check with invalid server URL
        """
        with pytest.raises(requests.exceptions.RequestException):
            # Use an invalid host to trigger connection error
            invalid_host = "http://invalid-host-that-does-not-exist:8000"
            requests.get(invalid_host, timeout=2)

    @pytest.mark.order(8)
    def test_auth_negative_invalid_credentials(self):
        """
        Test authentication with invalid credentials
        """
        invalid_credentials = {"username": "invalid", "password": "invalid"}
        try:
            response = self.client._post("/auth", data=invalid_credentials)
            # Either expect a non-200 status code or an error message in the response
            if response.status_code == 200:
                response_data = response.json()
                self.assertEqual(response_data["success"], False)
            else:
                self.assertNotEqual(response.status_code, 200)
        except requests.exceptions.RequestException:
            # If the request fails with an exception, the test passes
            pass

    @pytest.mark.order(9)
    def test_metadata_negative(self):
        """
        Test metadata API with invalid credentials
        """
        invalid_credentials = {"username": "invalid", "password": "invalid"}
        try:
            response = self.client._post("/metadata", data=invalid_credentials)
            # Either expect a non-200 status code or an error message in the response
            if response.status_code == 200:
                response_data = response.json()
                self.assertEqual(response_data["success"], False)
            else:
                self.assertNotEqual(response.status_code, 200)
        except requests.exceptions.RequestException:
            # If the request fails with an exception, the test passes
            pass

    @pytest.mark.order(10)
    def test_preflight_check_negative(self):
        """
        Test preflight check with invalid metadata
        """
        invalid_metadata = {"invalid": "metadata"}
        try:
            response = self.client._post(
                "/check",
                data={"credentials": self.credentials, "metadata": invalid_metadata},
            )
            # Either expect a non-200 status code or an error message in the response
            if response.status_code == 200:
                response_data = response.json()
                self.assertEqual(response_data["success"], False)
            else:
                self.assertNotEqual(response.status_code, 200)
        except requests.exceptions.RequestException:
            # If the request fails with an exception, the test passes
            pass

    @pytest.mark.order(11)
    def test_run_workflow_negative(self):
        """
        Test running workflow with invalid connection details
        """
        invalid_connection = {"invalid": "connection"}
        try:
            response = self.client._post(
                "/start",
                data={
                    "credentials": self.credentials,
                    "metadata": self.metadata,
                    "connection": invalid_connection,
                },
            )
            # Either expect a non-200 status code or an error message in the response
            if response.status_code == 200:
                response_data = response.json()
                self.assertEqual(response_data["success"], False)
            else:
                self.assertNotEqual(response.status_code, 200)
        except requests.exceptions.RequestException:
            # If the request fails with an exception, the test passes
            pass

    @pytest.mark.order(12)
    def test_workflow_status_negative(self):
        """
        Test getting workflow status with invalid workflow ID and run ID
        """
        invalid_workflow_id = "invalid-workflow-id"
        invalid_run_id = "invalid-run-id"
        try:
            response = self.client._get(
                f"/status/{invalid_workflow_id}/{invalid_run_id}"
            )
            # Either expect a non-200 status code or an error message in the response
            if response.status_code == 200:
                response_data = response.json()
                self.assertEqual(response_data["success"], False)
            else:
                self.assertNotEqual(response.status_code, 200)
        except requests.exceptions.RequestException:
            # If the request fails with an exception, the test passes
            pass
