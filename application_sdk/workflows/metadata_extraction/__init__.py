from typing import Any, Dict

from temporalio import workflow
from temporalio.common import RetryPolicy

from application_sdk.constants import ENABLE_ATLAN_UPLOAD
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.workflows import WorkflowInterface

logger = get_logger(__name__)


class MetadataExtractionWorkflow(WorkflowInterface):
    """Base workflow for metadata extraction."""

    async def run_exit_activities(self, workflow_args: Dict[str, Any]) -> None:
        """Run the exit activity for the workflow."""
        retry_policy = RetryPolicy(
            maximum_attempts=6,
            backoff_coefficient=2,
        )
        if ENABLE_ATLAN_UPLOAD:
            workflow_args["typename"] = "atlan-upload"
            await workflow.execute_activity_method(
                self.activities_cls.upload_to_atlan,
                args=[workflow_args],
                retry_policy=retry_policy,
                start_to_close_timeout=self.default_start_to_close_timeout,
                heartbeat_timeout=self.default_heartbeat_timeout,
            )
        else:
            logger.info("Atlan upload skipped for workflow (disabled)")
