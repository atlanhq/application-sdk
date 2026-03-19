from typing import Any, Dict

from temporalio import workflow
from temporalio.common import RetryPolicy

from application_sdk.constants import (
    ENABLE_ATLAN_UPLOAD,
    ENABLE_LAKEHOUSE_LOAD,
    LH_LOAD_TRANSFORMED_MODE,
    LH_LOAD_TRANSFORMED_NAMESPACE,
    LH_LOAD_TRANSFORMED_TABLE_NAME,
)
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.workflows import WorkflowInterface

logger = get_logger(__name__)


class MetadataExtractionWorkflow(WorkflowInterface):
    """Base workflow for metadata extraction."""

    async def _execute_lakehouse_load(
        self,
        workflow_args: Dict[str, Any],
        output_path: str,
        namespace: str,
        table_name: str,
        mode: str,
        file_extension: str,
    ) -> None:
        """Execute a single lakehouse load activity call."""
        load_config = {
            **workflow_args,
            "lh_load_config": {
                "output_path": output_path,
                "namespace": namespace,
                "table_name": table_name,
                "mode": mode,
                "file_extension": file_extension,
            },
        }
        retry_policy = RetryPolicy(maximum_attempts=6, backoff_coefficient=2)
        await workflow.execute_activity_method(
            self.activities_cls.load_to_lakehouse,
            args=[load_config],
            retry_policy=retry_policy,
            start_to_close_timeout=self.default_start_to_close_timeout,
            heartbeat_timeout=self.default_heartbeat_timeout,
        )

    async def run_exit_activities(self, workflow_args: Dict[str, Any]) -> None:
        """Run the exit activities for the workflow."""
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

        if (
            ENABLE_LAKEHOUSE_LOAD
            and LH_LOAD_TRANSFORMED_NAMESPACE
            and LH_LOAD_TRANSFORMED_TABLE_NAME
        ):
            output_path = workflow_args.get("output_path", "")
            await self._execute_lakehouse_load(
                workflow_args,
                output_path=f"{output_path}/transformed",
                namespace=LH_LOAD_TRANSFORMED_NAMESPACE,
                table_name=LH_LOAD_TRANSFORMED_TABLE_NAME,
                mode=LH_LOAD_TRANSFORMED_MODE,
                file_extension=".jsonl",
            )
        else:
            logger.info("Lakehouse load (transformed) skipped for workflow (disabled)")
