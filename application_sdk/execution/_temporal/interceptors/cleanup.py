import os
import shutil
import warnings
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Dict, List, Optional, Type

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.worker import (
    ExecuteWorkflowInput,
    Interceptor,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
)

from application_sdk.constants import CLEANUP_BASE_PATHS, TEMPORARY_PATH
from application_sdk.execution._temporal.activity_utils import build_output_path
from application_sdk.observability.logger_adaptor import get_logger

warnings.warn(
    "application_sdk.execution._temporal.interceptors.cleanup is deprecated. "
    "Post-run cleanup is now handled by App.on_complete() / App.cleanup_files(). "
    "CleanupInterceptor is no longer registered by default and will be removed in a future release.",
    DeprecationWarning,
    stacklevel=2,
)

logger = get_logger(__name__)
activity.logger = logger
workflow.logger = logger


@dataclass
class CleanupResult:
    """Result model for cleanup operations.

    Attributes:
        path_results: Cleanup results for each path (True=success, False=failure)
    """

    path_results: Dict[str, bool] = field(default_factory=dict)


@activity.defn
async def cleanup() -> CleanupResult:
    """Clean up temporary artifacts and activity state for the current workflow.

    Performs two types of cleanup:
    1. File cleanup: Removes all contents from configured base paths or default workflow directory
    2. State cleanup: Clears activity state for the current workflow (includes resource cleanup)

    Uses CLEANUP_BASE_PATHS constant or defaults to workflow-specific artifacts directory.

    Returns:
        CleanupResult: Structured cleanup results with path results and summary statistics.
    """
    path_results: Dict[str, bool] = {}
    base_paths: List[str] = [os.path.join(TEMPORARY_PATH, build_output_path())]

    # Use configured paths or default to workflow-specific artifacts directory
    if CLEANUP_BASE_PATHS:
        base_paths = CLEANUP_BASE_PATHS
        logger.info("Using CLEANUP_BASE_PATHS for cleanup: %s", base_paths)

    logger.info("Cleaning up all contents from base paths: %s", base_paths)

    for base_path in base_paths:
        try:
            if os.path.exists(base_path):
                if os.path.isdir(base_path):
                    # Remove entire directory and recreate it empty
                    shutil.rmtree(base_path)
                    logger.info("Cleaned up all contents from path: %s", base_path)
                    path_results[base_path] = True
                else:
                    logger.warning("Path is not a directory: %s", base_path)
                    path_results[base_path] = False
            else:
                logger.debug("Directory doesn't exist: %s", base_path)
                path_results[base_path] = True

        except Exception:
            logger.error(
                "Unexpected error cleaning up path: %s", base_path, exc_info=True
            )
            path_results[base_path] = False

    return CleanupResult(
        path_results=path_results,
    )


class CleanupWorkflowInboundInterceptor(WorkflowInboundInterceptor):
    """Interceptor for workflow-level app artifacts cleanup.

    This interceptor cleans up the entire app directory structure when the workflow
    completes or fails, following the pattern: base_path/appname/workflow_id/run_id
    Supports multiple base paths for comprehensive cleanup.
    """

    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        """Execute a workflow with app artifacts cleanup.

        Args:
            input (ExecuteWorkflowInput): The workflow execution input

        Returns:
            Any: The result of the workflow execution

        Raises:
            Exception: Re-raises any exceptions from workflow execution
        """
        output = None
        try:
            output = await self.next.execute_workflow(input)
        finally:
            # Always attempt cleanup regardless of workflow success/failure
            try:
                await workflow.execute_activity(
                    cleanup,
                    schedule_to_close_timeout=timedelta(minutes=5),
                    retry_policy=RetryPolicy(
                        maximum_attempts=3,
                    ),
                )

                logger.info("Cleanup completed successfully")

            except Exception:
                logger.warning("Failed to cleanup artifacts", exc_info=True)
                # Don't re-raise - cleanup failures shouldn't fail the workflow

        return output


class CleanupInterceptor(Interceptor):
    """Temporal interceptor for automatic app artifacts cleanup.

    This interceptor provides cleanup capabilities for application artifacts
    across multiple base paths following the pattern: base_path/appname/workflow_id/run_id

    Features:
    - Automatic cleanup of app-specific artifact directories
    - Cleanup on workflow completion or failure
    - Supports multiple cleanup paths via ATLAN_CLEANUP_BASE_PATHS env var
    - Simple activity-based cleanup logic
    - Comprehensive error handling and logging

    Example:
        >>> # Register the interceptor with Temporal worker
        >>> worker = Worker(
        ...     client,
        ...     task_queue="my-task-queue",
        ...     workflows=[MyWorkflow],
        ...     activities=[my_activity, cleanup],
        ...     interceptors=[CleanupInterceptor()]
        ... )

    Environment Configuration:
        >>> # Single path (default)
        >>> ATLAN_CLEANUP_BASE_PATHS="./local/tmp/artifacts/apps"

        >>> # Multiple paths (comma-separated)
        >>> ATLAN_CLEANUP_BASE_PATHS="./local/tmp/artifacts/apps,/storage/temp/apps,/shared/cleanup/apps"
    """

    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> Optional[Type[WorkflowInboundInterceptor]]:
        """Get the workflow interceptor class for cleanup.

        Args:
            input (WorkflowInterceptorClassInput): The interceptor input

        Returns:
            Optional[Type[WorkflowInboundInterceptor]]: The workflow interceptor class
        """
        return CleanupWorkflowInboundInterceptor
