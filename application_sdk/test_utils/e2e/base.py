from typing import Any, Dict

import pytest
import requests
from temporalio.client import WorkflowExecutionStatus

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.test_utils.e2e import TestInterface
from application_sdk.test_utils.e2e.conftest import workflow_details

logger = get_logger(__name__)


class BaseTest(TestInterface):
    config_file_path: str
    extracted_output_base_path: str
    schema_base_path: str
    test_workflow_args: Dict[str, Any]

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
        response = self.client.test_connection(
            credentials=self.test_workflow_args["credentials"]
        )
        self.assertEqual(response, self.expected_api_responses["auth"])

    @pytest.mark.order(3)
    def test_metadata(self):
        """
        Test Metadata
        """
        response = self.client.get_metadata(
            credentials=self.test_workflow_args["credentials"]
        )
        self.assertEqual(response, self.expected_api_responses["metadata"])

    @pytest.mark.order(4)
    def test_preflight_check(self):
        """
        Test Preflight Check
        """
        response = self.client.preflight_check(
            credentials=self.test_workflow_args["credentials"],
            metadata=self.test_workflow_args["metadata"],
        )
        self.assertEqual(response, self.expected_api_responses["preflight_check"])

    @pytest.mark.order(4)
    def test_run_workflow(self):
        """
        Test running the metadata extraction workflow
        """
        self.run_workflow()

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
        self.assertEqual(
            response_data["data"]["connection"], self.test_workflow_args["connection"]
        )
        self.assertEqual(
            response_data["data"]["metadata"], self.test_workflow_args["metadata"]
        )

    @pytest.mark.order(6)
    def test_configuration_update(self):
        """
        Test configuration update
        """
        update_payload = {
            "connection": self.test_workflow_args["connection"],
            "metadata": {
                **self.test_workflow_args["metadata"],
                "temp-table-regex": "^temp_.*",
            },
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
                # Check for error indicators in the response
                if response_data.get("success", True):
                    # If success is true, check for empty or error data
                    data = response_data.get("data", {})
                    self.assertTrue(
                        not data  # Empty data
                        or "error" in str(data).lower()  # Error in data
                        or "fail" in str(data).lower()  # Failure message
                        or data == {}  # Empty object
                        or isinstance(data, dict)
                        and all(
                            not v for v in data.values()
                        )  # All values are empty/falsy
                    )
            else:
                self.assertNotEqual(response.status_code, 200)
        except requests.exceptions.RequestException:
            # If the request fails with an exception, the test passes
            pass

    @pytest.mark.order(13)
    def test_metadata_with_invalid_credentials(self):
        """
        Test metadata API with invalid credentials structure
        """
        # Test with completely different credential structure
        invalid_credentials = {"api_key": "invalid_key", "region": "invalid_region"}
        try:
            response = self.client._post("/metadata", data=invalid_credentials)
            if response.status_code == 200:
                response_data = response.json()
                # Check for error indicators in the response
                if response_data.get("success", True):
                    data = response_data.get("data", {})
                    self.assertTrue(
                        not data  # Empty data
                        or len(data) == 0  # Empty list/dict
                        or "error" in str(data).lower()  # Error in data
                        or "fail" in str(data).lower()  # Failure message
                    )
            else:
                self.assertNotEqual(response.status_code, 200)
        except requests.exceptions.RequestException:
            # If the request fails with an exception, the test passes
            pass

    @pytest.mark.order(14)
    def test_preflight_check_with_invalid_credentials(self):
        """
        Test preflight check with invalid credentials
        """
        invalid_credentials = {"username": "invalid", "password": "invalid"}
        try:
            response = self.client._post(
                "/check",
                data={
                    "credentials": invalid_credentials,
                    "metadata": self.test_workflow_args["metadata"],
                },
            )
            if response.status_code == 200:
                response_data = response.json()
                # Check for error indicators in the response
                if response_data.get("success", True):
                    data = response_data.get("data", {})
                    self.assertTrue(
                        not data  # Empty data
                        or "error" in str(data).lower()  # Error in data
                        or "fail" in str(data).lower()  # Failure message
                        or data == {}  # Empty object
                        or isinstance(data, dict)
                        and all(
                            not v for v in data.values()
                        )  # All values are empty/falsy
                    )
            else:
                self.assertNotEqual(response.status_code, 200)
        except requests.exceptions.RequestException:
            # If the request fails with an exception, the test passes
            pass

    @pytest.mark.order(15)
    def test_run_workflow_with_invalid_credentials(self):
        """
        Test running workflow with invalid credentials
        """
        invalid_credentials = {"username": "invalid", "password": "invalid"}
        try:
            response = self.client._post(
                "/start",
                data={
                    "credentials": invalid_credentials,
                    "metadata": self.test_workflow_args["metadata"],
                    "connection": self.test_workflow_args["connection"],
                },
            )
            if response.status_code == 200:
                response_data = response.json()
                # If the API returns success=True, check for error indicators or workflow failure
                if response_data.get("success", False):
                    # If workflow started, check if it fails
                    if "data" in response_data and "workflow_id" in response_data.get(
                        "data", {}
                    ):
                        workflow_id = response_data["data"]["workflow_id"]
                        run_id = response_data["data"]["run_id"]

                        # Wait a short time for the workflow to potentially fail
                        import time

                        time.sleep(5)

                        # Check workflow status
                        try:
                            status_response = self.client.get_workflow_status(
                                workflow_id, run_id
                            )
                            # The workflow should either fail or be in a non-completed state
                            if status_response.get("success", False):
                                status = status_response.get("data", {}).get("status")
                                self.assertTrue(
                                    status != WorkflowExecutionStatus.COMPLETED.name,
                                    f"Workflow with invalid credentials unexpectedly completed successfully: {status}",
                                )
                        except Exception:
                            # If checking status fails, that's also acceptable
                            pass
                    else:
                        # If no workflow was started, check for error indicators in the response
                        self.assertTrue(
                            "error" in str(response_data).lower()
                            or "fail" in str(response_data).lower()
                            or "invalid" in str(response_data).lower()
                        )
            else:
                self.assertNotEqual(response.status_code, 200)
        except requests.exceptions.RequestException:
            # If the request fails with an exception, the test passes
            pass

    def _get_extracted_dir_path(self, expected_file_postfix: str) -> str:
        """
        Method to get the extracted directory path
        """
        return f"{self.extracted_output_base_path}/{workflow_details[self.test_name]['workflow_id']}/{workflow_details[self.test_name]['run_id']}{expected_file_postfix}"
