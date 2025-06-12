"""
Base test class for publish workflow testing.
"""

import os
import time
from abc import abstractmethod
from typing import Any, Dict, List, Optional

import daft
import pandas as pd
from app.models.publish_state import PublishStateRecord
from pandera.io import from_yaml
from temporalio.client import WorkflowExecutionStatus

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.test_utils.e2e import TestInterface
from application_sdk.test_utils.e2e.client import APIServerClient
from application_sdk.test_utils.e2e.conftest import workflow_details
from application_sdk.test_utils.e2e.utils import load_config_from_yaml

logger = get_logger(__name__)


class WorkflowExecutionError(Exception):
    """Exception class for raising exceptions during workflow execution"""


class PublishBaseTest(TestInterface):
    """Base class for publish workflow testing.

    This class provides the core functionality for testing publish workflows
    across different scenarios. It inherits from TestInterface but only
    implements the methods relevant to publish workflow testing.
    """

    config_file_path: str
    schema_base_path: str
    workflow_timeout: Optional[int] = 300
    polling_interval: int = 10

    @classmethod
    def setup_class(cls):
        """Set up the test class by preparing paths and loading configuration."""
        cls.prepare_dir_paths()
        config = load_config_from_yaml(yaml_file_path=cls.config_file_path)

        # Load scenario-specific configuration
        cls.scenario_config = config.get("scenario_config", {})
        cls.test_name = config["test_name"]
        cls.workflow_config = config.get("workflow_config", {})
        cls.client = APIServerClient(
            host=config["server_config"]["server_host"],
            version=config["server_config"]["server_version"],
        )
        cls.test_name = config["test_name"]

    def run_scenario_test(self, scenario_name: str):
        """Run a specific test scenario."""
        logger.info(f"Running scenario: {scenario_name}")

        # Generate and set up test data
        test_data = self.generate_test_data(scenario_name, self.scenario_config)

        # Run the workflow
        response = self._run_workflow(
            publish_state_prefix=test_data["publish_state_prefix"],
            transformed_data_prefix=test_data["transformed_data_prefix"],
            current_state_prefix=test_data["current_state_prefix"],
            connection_qualified_name=test_data["connection_qualified_name"],
        )

        # Verify workflow started successfully
        self.assertEqual(response["success"], True)
        self.assertEqual(response["message"], "Workflow started successfully")

        # Store workflow details for monitoring
        workflow_details[self.test_name] = {
            "workflow_id": response["data"]["workflow_id"],
            "run_id": response["data"]["run_id"],
        }

        # Monitor workflow execution
        workflow_status = self.monitor_and_wait_workflow_execution()

        # Check workflow completion
        if workflow_status != WorkflowExecutionStatus.COMPLETED.name:
            raise WorkflowExecutionError(
                f"Workflow failed with status: {workflow_status}"
            )

        # Validate workflow output
        self.validate_scenario_output(scenario_name)
        logger.info(f"Scenario {scenario_name} completed successfully")

    @abstractmethod
    def generate_test_data(
        self, scenario_name: str, scenario_config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Generate test data based on scenario configuration.

        Args:
            scenario_name (str): Name of the test scenario
            scenario_config (Dict[str, Any]): Configuration for the scenario

        Returns:
            Dict[str, Any]: Dictionary containing:
                - publish_state_prefix: Path to publish state directory
                - transformed_data_prefix: Path to transformed data directory
                - current_state_prefix: Path to current state directory
                - connection_qualified_name: Connection qualified name for the test
        """
        pass

    def validate_scenario_output(self, scenario_name: str):
        """Validate the workflow output against scenario schema."""
        logger.info(f"Validating output for scenario: {scenario_name}")

        schema_file = os.path.join(self.schema_base_path, f"publish_output_schema.yaml")
        schema = from_yaml(schema_file)
        output_data = self._get_workflow_output()
        schema.validate(output_data)

        self._validate_scenario_specific(scenario_name, output_data)

    def monitor_and_wait_workflow_execution(self) -> str:
        """Monitor workflow execution until completion."""
        start_time = time.time()
        while True:
            workflow_status_response = self.client.get_workflow_status(
                workflow_details[self.test_name]["workflow_id"],
                workflow_details[self.test_name]["run_id"],
            )

            self.run_id = workflow_status_response["data"]["last_executed_run_id"]
            self.assertEqual(workflow_status_response["success"], True)
            current_status = workflow_status_response["data"]["status"]

            if current_status != WorkflowExecutionStatus.RUNNING.name:
                return current_status

            if (
                self.workflow_timeout
                and (time.time() - start_time) > self.workflow_timeout
            ):
                raise TimeoutError(
                    f"Workflow did not complete within {self.workflow_timeout} seconds"
                )

            time.sleep(self.polling_interval)

    def _run_workflow(
        self,
        publish_state_prefix: str,
        transformed_data_prefix: str,
        current_state_prefix: str,
        connection_qualified_name: str,
        partitioning_strategy: str = "dynamic",
        chunk_size: int = 50000,
    ) -> Dict[str, Any]:
        """Run the workflow via the /start API."""
        response = self.client._post(
            "/start",
            data={
                "publish_state_prefix": publish_state_prefix,
                "transformed_data_prefix": transformed_data_prefix,
                "current_state_prefix": current_state_prefix,
                "connection_qualified_name": connection_qualified_name,
                "partitioning_strategy": partitioning_strategy,
                "chunk_size": chunk_size,
            },
        )
        assert response.status_code == 200
        return response.json()

    def _get_workflow_output(self) -> pd.DataFrame:
        """Get the workflow output data."""
        # Implementation depends on how workflow output is stored
        pass

    def _validate_scenario_specific(
        self, scenario_name: str, output_data: pd.DataFrame
    ):
        """Perform scenario-specific validation."""
        # Implementation depends on scenario-specific requirements
        pass

    # TestInterface methods not implemented as they're not needed
    def test_auth(self):
        pass

    def test_metadata(self):
        pass

    def test_preflight_check(self):
        pass
