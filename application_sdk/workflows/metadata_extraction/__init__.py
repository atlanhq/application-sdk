from typing import Any, Dict

from temporalio import workflow
from temporalio.common import RetryPolicy

from application_sdk.constants import ENABLE_ATLAN_UPLOAD, ENABLE_LAKEHOUSE_LOAD
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.workflows import WorkflowInterface
from application_sdk.workflows.metadata_extraction.lakehouse import LakehouseLoadMixin

logger = get_logger(__name__)


class MetadataExtractionWorkflow(LakehouseLoadMixin, WorkflowInterface):
    """Base workflow for metadata extraction.

    Lakehouse loading is provided by LakehouseLoadMixin and controlled
    via environment variables (ENABLE_LAKEHOUSE_LOAD, LH_LOAD_*).
    """

    async def run_exit_activities(self, workflow_args: Dict[str, Any]) -> None:
        """Run post-extraction activities: upload to Atlan + lakehouse load."""
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

        if ENABLE_LAKEHOUSE_LOAD:
            await self.load_transformed_to_lakehouse(workflow_args)
        else:
            logger.info("Lakehouse load skipped for workflow (disabled)")
