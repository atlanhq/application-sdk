from typing import Any, Dict

import pytest
import requests

from application_sdk.common.logger_adaptors import get_logger
from application_sdk.test_utils.e2e import TestInterface

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

    @pytest.mark.order(1)
    def test_health_check(self):
        """
        Check if the server is up and running and is responding to requests
        """
        response = requests.get(self.client.host)
        self.assertEqual(response.status_code, 200)

    @pytest.mark.order(2)
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