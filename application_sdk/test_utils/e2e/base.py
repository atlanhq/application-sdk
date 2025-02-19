import time
from typing import Any, Dict

import pytest
import requests

from application_sdk.common.logger_adaptors import get_logger
from application_sdk.test_utils.e2e import TestInterface
from application_sdk.test_utils.e2e.conftest import WorkflowDetails

logger = get_logger(__name__)


class BaseTest(TestInterface):
    config_file_path: str
    api_response_path: str
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
        workflow_id = response["data"]["workflow_id"]
        run_id = response["data"]["run_id"]

        # Wait for the workflow to complete
        start_time = time.time()
        while True:
            workflow_status_response = self.client.get_workflow_status(
                workflow_id, run_id
            )
            self.assertEqual(workflow_status_response["success"], True)
            if workflow_status_response["data"]["status"].lower() == "completed":
                WorkflowDetails.workflow_id = workflow_status_response["data"][
                    "workflow_id"
                ]
                WorkflowDetails.run_id = workflow_status_response["data"]["run_id"]
                break
            if time.time() - start_time > self.workflow_timeout:
                raise TimeoutError("Workflow did not complete in the expected time")
            time.sleep(2)

        logger.info("Workflow completed successfully")

    @pytest.mark.order(6)
    def test_data_validation(self):
        """
        Test for validating the extracted source data
        """
        self.validate_data()
